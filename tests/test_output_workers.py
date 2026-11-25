# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
"""Generating the outputs of one manifests repository side by side.

These tests run manifest-builder for real, in worker processes, against local
repositories: a config repository whose plugin records when and where each
output generated, a bare manifests repository standing in for GitHub, and the
change or diff relcoord makes between them.
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from dulwich import porcelain
from dulwich.object_store import iter_tree_contents
from dulwich.objects import Blob, Commit, Tree
from dulwich.refs import HEADREF
from dulwich.refs import Ref as GitRef
from dulwich.repo import Repo
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from relcoord import change, output_workers, tracing
from relcoord.change import (
    ChangeProcessingError,
    ChangeProcessor,
    DiffCommentProcessor,
)
from relcoord.config import (
    IdcatSettings,
    OutputSettings,
    RolloutSettings,
    RolloutStage,
)
from relcoord.output_workers import OutputWorkers
from relcoord.validator import Validation

AUTHOR = b"Test <test@example.com>"
MAIN = GitRef(b"refs/heads/main")

# A config block that writes one ConfigMap per output and records, in the log
# directory, when it started and finished and in which process. Variables
# decide what it does for a given cluster: wait until a number of outputs are
# generating at once, take a while, fail, or kill its process.
PROBE_PLUGIN = """
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from manifest_builder.blocks import ConfigBlock
from manifest_builder.output import write_documents


@dataclass
class ProbeConfig:
    name: str
    namespace: str | None
    cluster: str
    log: str
    rendezvous: int = 0
    rendezvous_on: list = field(default_factory=list)
    delays: dict = field(default_factory=dict)
    fail_on: list = field(default_factory=list)
    exit_on: list = field(default_factory=list)


class ProbeBlock(ConfigBlock[ProbeConfig]):
    def __init__(self):
        self.configs = []

    def top_level_config_name(self):
        return "probe"

    def load_config(self, data, source_file, root_config, default_namespace=None,
                    default_image=None):
        variables = root_config.get("variables") or {}
        for item in data:
            self.configs.append(ProbeConfig(
                name=item["name"],
                namespace=item.get("namespace", default_namespace),
                cluster=str(variables["cluster"]),
                log=item["log"],
                rendezvous=item.get("rendezvous", 0),
                rendezvous_on=item.get("rendezvous_on", []),
                delays=item.get("delays", {}),
                fail_on=item.get("fail_on", []),
                exit_on=item.get("exit_on", []),
            ))

    def iter_configs(self):
        return self.configs

    def validate(self, config, repo_root):
        pass

    def generate(self, config, context):
        log = Path(config.log)
        start = time.time()
        (log / f"{config.cluster}.start").write_text(f"{start} {os.getpid()}")
        if config.rendezvous and (
            not config.rendezvous_on or config.cluster in config.rendezvous_on
        ):
            while len(list(log.glob("*.start"))) < config.rendezvous:
                if time.time() > start + 30:
                    raise RuntimeError(f"{config.cluster} generated alone")
                time.sleep(0.01)
        time.sleep(config.delays.get(config.cluster, 0))
        if config.cluster in config.exit_on:
            os._exit(3)
        if config.cluster in config.fail_on:
            raise RuntimeError(f"probe refused {config.cluster}")
        written = write_documents(
            [{
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": config.name, "namespace": config.namespace},
                "data": {"cluster": config.cluster},
            }],
            context.output_dir,
            config.namespace,
            config.name,
        )
        (log / f"{config.cluster}.end").write_text(f"{time.time()} {os.getpid()}")
        return written
"""


@dataclass(frozen=True)
class Run:
    """When and where one output's generation ran."""

    start: float
    end: float | None
    pid: int


def _commit_all(path: Path, files: Mapping[str, str], message: bytes) -> bytes:
    for name, content in files.items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(content)
    repo = Repo(str(path))
    try:
        porcelain.add(repo, [name.encode() for name in files])
        return porcelain.commit(repo, message, author=AUTHOR, committer=AUTHOR)
    finally:
        repo.close()


class Repositories:
    """A config repository and a manifests repository, both local."""

    def __init__(self, tmp_path: Path, manifests: str = "manifests") -> None:
        self.tmp_path = tmp_path
        self.log = tmp_path / "log"
        self.log.mkdir(parents=True, exist_ok=True)
        self.config = tmp_path / "config"
        if not self.config.exists():
            porcelain.init(str(self.config)).close()
            self.config_commit = b""
        self.remote = tmp_path / f"{manifests}.git"
        porcelain.init(str(self.remote), bare=True).close()
        seed = tmp_path / f"{manifests}-seed"
        porcelain.init(str(seed)).close()
        _commit_all(
            seed,
            {
                "README.md": "manifests\n",
                ".gitignore": "*.tmp\n",
                # A previous system run owned demo and gone in a, so generating
                # a prunes both; another owner's namespace there stays as it is.
                "a/owners/system.toml": 'owned = ["demo", "gone"]\n',
                "a/demo/old.yaml": "kind: Old\n",
                "a/gone/stale.yaml": "kind: Stale\n",
                "a/owners/team.toml": 'owned = "team"\n',
                "a/team/theirs.yaml": "kind: Theirs\n",
                "untouched/demo/keep.yaml": "kind: Keep\n",
            },
            b"seed",
        )
        repo = Repo(str(seed))
        try:
            (_, branch), _ = repo.refs.follow(HEADREF)
        finally:
            repo.close()
        assert branch is not None
        porcelain.push(
            str(seed),
            str(self.remote),
            branch + b":refs/heads/main",
            errstream=BytesIO(),
        )
        remote = Repo(str(self.remote))
        try:
            remote.refs.set_symbolic_ref(HEADREF, MAIN)
            self.seed = remote.refs[MAIN]
        finally:
            remote.close()

    def write_config(self, **probe: Any) -> str:
        """Commit a config generating a probe for every output, returning the commit."""
        settings = {"name": "probe", "namespace": "demo", "log": str(self.log), **probe}
        lines = ["[[probe]]"] + [
            f"{key} = {_toml(value)}" for key, value in settings.items()
        ]
        commit = _commit_all(
            self.config,
            {"config.toml": "\n".join(lines) + "\n", "plugins/probe.py": PROBE_PLUGIN},
            b"probe config",
        )
        return commit.decode()

    def outputs(self, *directories: str) -> list[OutputSettings]:
        return [
            OutputSettings(
                name=directory.replace("/", "-"),
                repository=str(self.remote),
                directory=Path(directory),
                vars={"cluster": directory.replace("/", "-")},
            )
            for directory in directories
        ]

    def runs(self) -> dict[str, Run]:
        runs: dict[str, Run] = {}
        for start in self.log.glob("*.start"):
            started, pid = start.read_text().split()
            end = start.with_suffix(".end")
            runs[start.stem] = Run(
                start=float(started),
                end=float(end.read_text().split()[0]) if end.exists() else None,
                pid=int(pid),
            )
        return runs

    def history(self) -> list[Commit]:
        """The commits the remote's main gained on top of the seed, oldest first."""
        remote = Repo(str(self.remote))
        try:
            commits: list[Commit] = []
            sha = remote.refs[MAIN]
            while sha != self.seed:
                commit = remote[sha]
                assert isinstance(commit, Commit)
                commits.append(commit)
                sha = commit.parents[0]
            return list(reversed(commits))
        finally:
            remote.close()

    def files(self) -> dict[str, bytes]:
        remote = Repo(str(self.remote))
        try:
            tip = remote[remote.refs[MAIN]]
            assert isinstance(tip, Commit)
            files: dict[str, bytes] = {}
            for entry in iter_tree_contents(remote.object_store, tip.tree):
                assert entry.path is not None and entry.sha is not None
                blob = remote[entry.sha]
                assert isinstance(blob, Blob)
                files[entry.path.decode()] = blob.data
            return files
        finally:
            remote.close()


def _toml(value: object) -> str:
    if isinstance(value, str):
        return f'"{value}"'
    if isinstance(value, list):
        return "[" + ", ".join(_toml(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f'"{k}" = {_toml(v)}' for k, v in value.items()) + "}"
    return str(value)


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Where a change makes its workspace, so a test can see it is gone."""
    workdir = tmp_path / "work"
    workdir.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(workdir))
    yield workdir


def _change(
    repositories: Repositories,
    outputs: Sequence[OutputSettings],
    commit: str,
    workers: OutputWorkers | None,
    **settings: Any,
) -> Any:
    return ChangeProcessor(outputs=outputs, output_workers=workers, **settings).process(
        str(repositories.config), commit, None, system=True
    )


def _overlapping(runs: Mapping[str, Run]) -> int:
    """The most generations that ran at the same moment."""
    edges = sorted(
        [(run.start, 1) for run in runs.values()]
        + [(run.end or run.start, -1) for run in runs.values()]
    )
    most = current = 0
    for _, step in edges:
        current += step
        most = max(most, current)
    return most


def test_outputs_generate_at_once_in_worker_processes(
    tmp_path: Path, workdir: Path
) -> None:
    repositories = Repositories(tmp_path)
    # Each probe waits until all three are generating, so a run that generated
    # them one after another would time out instead of passing.
    commit = repositories.write_config(rendezvous=3)
    outputs = repositories.outputs("a", "b", "c")

    result = _change(repositories, outputs, commit, OutputWorkers(3))

    runs = repositories.runs()
    assert sorted(runs) == ["a", "b", "c"]
    assert len({run.pid for run in runs.values()}) == 3
    assert os.getpid() not in {run.pid for run in runs.values()}
    assert max(run.start for run in runs.values()) < min(
        run.end or 0 for run in runs.values()
    )
    history = repositories.history()
    assert [_output_path(commit) for commit in history] == ["a", "b", "c"]
    files = repositories.files()
    assert files["a/demo/configmap-probe.yaml"].count(b"cluster: a") == 1
    assert "a/demo/old.yaml" not in files
    assert files["untouched/demo/keep.yaml"] == b"kind: Keep\n"
    assert files["README.md"] == b"manifests\n"
    assert [output.name for output in result.outputs] == ["a", "b", "c"]
    assert list(workdir.iterdir()) == []


def test_outputs_generated_at_once_commit_what_one_after_another_does(
    tmp_path: Path, workdir: Path
) -> None:
    serial = Repositories(tmp_path / "serial")
    concurrent = Repositories(tmp_path / "concurrent")
    outputs_serial = serial.outputs("a", "b", "nested/c")
    outputs_concurrent = concurrent.outputs("a", "b", "nested/c")

    _change(serial, outputs_serial, serial.write_config(), None)
    _change(concurrent, outputs_concurrent, concurrent.write_config(), OutputWorkers(3))

    serial_history = serial.history()
    concurrent_history = concurrent.history()
    assert [commit.tree for commit in concurrent_history] == [
        commit.tree for commit in serial_history
    ]
    assert [_without_config_commit(c) for c in concurrent_history] == [
        _without_config_commit(c) for c in serial_history
    ]
    assert concurrent.files() == serial.files()


def test_outputs_generate_one_after_another_in_process_by_default(
    tmp_path: Path, workdir: Path
) -> None:
    repositories = Repositories(tmp_path)
    commit = repositories.write_config(delays={"a": 0.2, "b": 0.2})

    _change(repositories, repositories.outputs("a", "b", "c"), commit, None)

    runs = repositories.runs()
    assert {run.pid for run in runs.values()} == {os.getpid()}
    assert _overlapping(runs) == 1
    assert [_output_path(c) for c in repositories.history()] == ["a", "b", "c"]


@pytest.mark.parametrize(
    "directories", [("a", "a/inner"), ("a", "a"), (".", "a")], ids=str
)
def test_outputs_whose_directories_overlap_generate_one_after_another(
    tmp_path: Path,
    workdir: Path,
    caplog: pytest.LogCaptureFixture,
    directories: tuple[str, str],
) -> None:
    repositories = Repositories(tmp_path)
    commit = repositories.write_config()
    outputs = [
        OutputSettings(
            name=f"out{index}",
            repository=str(repositories.remote),
            directory=Path(directory),
            vars={"cluster": f"out{index}"},
        )
        for index, directory in enumerate(directories)
    ]

    with caplog.at_level(logging.INFO, logger="relcoord.output_workers"):
        _change(repositories, outputs, commit, OutputWorkers(2))

    assert {run.pid for run in repositories.runs().values()} == {os.getpid()}
    assert "write overlapping directories" in caplog.text


def test_an_output_directory_through_a_symlink_generates_one_after_another(
    tmp_path: Path, workdir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    repositories = Repositories(tmp_path)
    remote = Repo(str(repositories.remote))
    try:
        # linked -> a, committed as the symlink git stores, mode 120000.
        target = Blob.from_string(b"a")
        seed = remote[remote.refs[MAIN]]
        assert isinstance(seed, Commit)
        tree = remote[seed.tree]
        assert isinstance(tree, Tree)
        tree.add(b"linked", 0o120000, target.id)
        linked = Commit()
        linked.tree = tree.id
        linked.parents = [seed.id]
        linked.author = linked.committer = AUTHOR
        linked.author_time = linked.commit_time = seed.commit_time
        linked.author_timezone = linked.commit_timezone = 0
        linked.message = b"link"
        for obj in (target, tree, linked):
            remote.object_store.add_object(obj)
        remote.refs[MAIN] = linked.id
        repositories.seed = linked.id
    finally:
        remote.close()
    commit = repositories.write_config()

    with caplog.at_level(logging.INFO, logger="relcoord.output_workers"):
        _change(
            repositories,
            repositories.outputs("b", "linked/x"),
            commit,
            OutputWorkers(2),
        )

    assert {run.pid for run in repositories.runs().values()} == {os.getpid()}
    assert "which is not a directory" in caplog.text


def test_a_failed_output_pushes_nothing_and_leaves_no_workspace(
    tmp_path: Path, workdir: Path
) -> None:
    repositories = Repositories(tmp_path)
    commit = repositories.write_config(fail_on=["b"])

    with pytest.raises(ChangeProcessingError, match="probe refused b"):
        _change(
            repositories, repositories.outputs("a", "b", "c"), commit, OutputWorkers(3)
        )

    assert repositories.history() == []
    assert list(workdir.iterdir()) == []


def test_the_failure_raised_is_the_first_in_configured_order(
    tmp_path: Path, workdir: Path
) -> None:
    repositories = Repositories(tmp_path)
    # c fails long before a does.
    commit = repositories.write_config(fail_on=["a", "c"], delays={"a": 0.5})

    with pytest.raises(ChangeProcessingError, match="probe refused a"):
        _change(
            repositories, repositories.outputs("a", "b", "c"), commit, OutputWorkers(3)
        )

    assert repositories.history() == []


def test_a_failure_stops_outputs_not_started_yet(tmp_path: Path, workdir: Path) -> None:
    repositories = Repositories(tmp_path)
    commit = repositories.write_config(fail_on=["a"], delays={"b": 0.5})

    with pytest.raises(ChangeProcessingError, match="probe refused a"):
        _change(
            repositories,
            repositories.outputs("a", "b", "c", "d"),
            commit,
            OutputWorkers(2),
        )

    runs = repositories.runs()
    assert sorted(runs) == ["a", "b"]
    # b was generating when a failed, and was let finish.
    assert runs["b"].end is not None


def test_a_worker_process_that_dies_fails_the_change_without_a_push(
    tmp_path: Path, workdir: Path
) -> None:
    repositories = Repositories(tmp_path)
    commit = repositories.write_config(exit_on=["b"])

    with pytest.raises(ChangeProcessingError, match="worker process ended"):
        _change(repositories, repositories.outputs("a", "b"), commit, OutputWorkers(2))

    assert repositories.history() == []
    assert list(workdir.iterdir()) == []


def test_workers_bound_generations_across_repositories(
    tmp_path: Path, workdir: Path
) -> None:
    first = Repositories(tmp_path, manifests="first")
    second = Repositories(tmp_path, manifests="second")
    commit = first.write_config(delays={name: 0.2 for name in "abcdwxyz"})
    outputs = first.outputs("a", "b", "c", "d") + second.outputs("w", "x", "y", "z")

    _change(first, outputs, commit, OutputWorkers(2), max_concurrency=2)

    runs = first.runs()
    assert sorted(runs) == sorted("abcdwxyz")
    assert _overlapping(runs) == 2
    assert [_output_path(c) for c in first.history()] == ["a", "b", "c", "d"]
    assert [_output_path(c) for c in second.history()] == ["w", "x", "y", "z"]


class RecordingDetector:
    """Takes a while to see each deployment, so the next stage generates meanwhile."""

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.waited: list[set[str]] = []
        self.ended: list[float] = []

    def wait_for_success(
        self, *, created_or_modified: Mapping[Any, str], removed: set[Any]
    ) -> None:
        self.waited.append({ref.name for ref in created_or_modified})
        time.sleep(self.delay)
        self.ended.append(time.time())


@pytest.mark.parametrize("generate_while_waiting", [False, True])
def test_a_rollout_pushes_its_stages_in_order(
    tmp_path: Path, workdir: Path, generate_while_waiting: bool
) -> None:
    repositories = Repositories(tmp_path)
    # b and c each wait for the other to start: a's start counts too.
    commit = repositories.write_config(rendezvous=3, rendezvous_on=["b", "c"])
    outputs = repositories.outputs("a", "b", "c")
    pushes: list[list[str]] = []
    detector = RecordingDetector(delay=0.5)
    original_push = change._push_repository

    def recording_push(
        checkout: Path, remote: str, idcat: IdcatSettings | None
    ) -> None:
        original_push(checkout, remote, idcat)
        pushes.append([_output_path(c) for c in repositories.history()])

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(change, "_push_repository", recording_push)
        _change(
            repositories,
            outputs,
            commit,
            OutputWorkers(2),
            rollouts=[
                RolloutSettings(
                    name="linear",
                    stages=(
                        RolloutStage(outputs=("a",)),
                        RolloutStage(outputs=("b", "c")),
                    ),
                )
            ],
            detect_deployment=True,
            deployment_detector=detector,
            generate_while_waiting=generate_while_waiting,
        )

    assert pushes == [["a"], ["a", "b", "c"]]
    assert detector.waited == [{"demo", "probe"}] * 3
    runs = repositories.runs()
    assert len({runs["b"].pid, runs["c"].pid, os.getpid()}) == 3
    a_observed = detector.ended[0]
    if generate_while_waiting:
        # The second stage started generating while a was waited for.
        assert min(runs["b"].start, runs["c"].start) < a_observed
    else:
        assert min(runs["b"].start, runs["c"].start) > a_observed


class TreeRecordingValidator:
    def __init__(self) -> None:
        self.trees: dict[str, dict[str, bytes]] = {}
        self._lock = threading.Lock()

    def validate(
        self,
        files: Mapping[str, bytes],
        checks: Sequence[str] = (),
        *,
        progress: Callable[..., None] | None = None,
    ) -> Validation:
        tree = files
        cluster = next(
            line.split(b": ")[1].decode()
            for content in tree.values()
            for line in content.splitlines()
            if line.strip().startswith(b"cluster:")
        )
        with self._lock:
            self.trees[cluster] = dict(tree)
        return Validation(passed=True, digest=f"sha256:{cluster}")


def test_a_diff_generated_at_once_matches_one_generated_after_another(
    tmp_path: Path, workdir: Path
) -> None:
    results = {}
    trees = {}
    for name, workers in (("serial", None), ("concurrent", OutputWorkers(3))):
        repositories = Repositories(tmp_path / name)
        commit = repositories.write_config()
        validator = TreeRecordingValidator()
        results[name] = DiffCommentProcessor(
            outputs=repositories.outputs("a", "b", "c"),
            validator=validator,
            output_workers=workers,
        ).diff(str(repositories.config), commit, system=True)
        trees[name] = validator.trees
        assert repositories.history() == []

    serial, concurrent = results["serial"], results["concurrent"]
    assert _normalised(
        concurrent.diffs[0].manifest_diff.diff, "concurrent"
    ) == _normalised(serial.diffs[0].manifest_diff.diff, "serial")
    assert [v.output for v in concurrent.validations] == ["a", "b", "c"]
    assert trees["concurrent"] == trees["serial"]
    assert sorted(trees["concurrent"]) == ["a", "b", "c"]
    assert list(workdir.iterdir()) == []


def test_generate_spans_overlap_under_the_request(
    tmp_path: Path, workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(tracing, "_tracer", provider.get_tracer("test"))
    repositories = Repositories(tmp_path)
    commit = repositories.write_config(rendezvous=2)

    _change(repositories, repositories.outputs("a", "b"), commit, OutputWorkers(2))

    spans = {span.name: span for span in exporter.get_finished_spans()}
    a, b = spans["generate manifests a"], spans["generate manifests b"]
    request = spans["change.process"]
    assert a.parent is not None and b.parent is not None
    assert a.context.trace_id == b.context.trace_id == request.context.trace_id
    assert a.start_time is not None and b.start_time is not None
    assert a.end_time is not None and b.end_time is not None
    assert max(a.start_time, b.start_time) < min(a.end_time, b.end_time)
    provider.shutdown()


def test_cone_patterns_check_out_a_nested_directory() -> None:
    assert output_workers._cone_patterns(("a", "b", "c")) == (
        "/*\n!/*/\n/a/\n!/a/*/\n/a/b/\n!/a/b/*/\n/a/b/c/\n"
    )


def test_a_scratch_repository_checks_out_only_its_directory(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    porcelain.init(str(checkout)).close()
    base = _commit_all(
        checkout,
        {
            "README.md": "root\n",
            "nested/.gitignore": "*.tmp\n",
            "nested/sibling/x.yaml": "x\n",
            "nested/c/y.yaml": "y\n",
            "other/z.yaml": "z\n",
        },
        b"base",
    ).decode()
    scratch = tmp_path / "scratch"

    output_workers.create_scratch(checkout, base, scratch, "nested/c")

    on_disk = sorted(
        path.relative_to(scratch).as_posix()
        for path in scratch.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(scratch).parts
    )
    # dulwich matches cones a top-level directory at a time, so the rest of
    # nested comes along; nothing outside it does.
    assert {"README.md", "nested/.gitignore", "nested/c/y.yaml"} <= set(on_disk)
    assert "other/z.yaml" not in on_disk
    repo = Repo(str(scratch))
    try:
        assert repo.head().decode() == base
        assert porcelain.status(repo).unstaged == []
        assert len(repo.open_index()) == 5
    finally:
        repo.close()


def test_integrating_an_output_that_removed_its_directory_drops_it(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "checkout"
    porcelain.init(str(checkout)).close()
    base = _commit_all(
        checkout, {"a/x.yaml": "x\n", "b/y.yaml": "y\n"}, b"base"
    ).decode()
    scratch = tmp_path / "scratch"
    output_workers.create_scratch(checkout, base, scratch, "a")
    (scratch / "a" / "x.yaml").unlink()
    repo = Repo(str(scratch))
    try:
        porcelain.add(repo, [b"a/x.yaml"])
        porcelain.commit(repo, b"remove a", author=AUTHOR, committer=AUTHOR)
    finally:
        repo.close()
    output = OutputSettings(name="a", repository="r", directory=Path("a"))

    assert output_workers._integrate(checkout, scratch, base, output) is True

    repo = Repo(str(checkout))
    try:
        head = repo[repo.head()]
        assert isinstance(head, Commit)
        assert head.message == b"remove a"
        assert head.parents == [base.encode()]
        tree = repo[head.tree]
        assert isinstance(tree, Tree)
        assert [entry.path for entry in tree.items()] == [b"b"]
    finally:
        repo.close()


def test_integrating_refuses_a_commit_outside_the_output_directory(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "checkout"
    porcelain.init(str(checkout)).close()
    base = _commit_all(
        checkout, {"a/x.yaml": "x\n", "b/y.yaml": "y\n"}, b"base"
    ).decode()
    scratch = tmp_path / "scratch"
    output_workers.create_scratch(checkout, base, scratch, "a")
    repo = Repo(str(scratch))
    try:
        index = repo.open_index()
        # Stand in for a generation that strayed: drop b from what it commits.
        del index[b"b/y.yaml"]
        index.write()
        porcelain.commit(repo, b"strayed", author=AUTHOR, committer=AUTHOR)
    finally:
        repo.close()
    output = OutputSettings(name="a", repository="r", directory=Path("a"))

    with pytest.raises(
        RuntimeError, match="committed outside its directory a: b/y.yaml"
    ):
        output_workers._integrate(checkout, scratch, base, output)


def test_refreshing_the_config_index_leaves_workers_nothing_to_write(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config"
    porcelain.init(str(config)).close()
    _commit_all(config, {"config.toml": "a = 1\n"}, b"config")
    index = config / ".git" / "index"

    def stale() -> None:
        # Same content, new mtime: the index's stat information goes stale.
        future = time.time() + 5
        os.utime(config / "config.toml", (future, future))

    stale()
    before = index.stat().st_mtime_ns
    porcelain.status(str(config))
    assert index.stat().st_mtime_ns != before, "status should rewrite a stale index"

    stale()
    output_workers._refresh_index(config)
    refreshed = index.stat().st_mtime_ns
    porcelain.status(str(config))
    assert index.stat().st_mtime_ns == refreshed


def test_workers_need_a_limit_of_at_least_one() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        OutputWorkers(0)


def _output_path(commit: Commit) -> str:
    for line in commit.message.decode().splitlines():
        if line.startswith("Output path: "):
            return line.removeprefix("Output path: ")
    raise AssertionError(f"no output path in {commit.message!r}")


def _without_config_commit(commit: Commit) -> list[str]:
    """A manifests commit's message, without what differs by config repository."""
    return [
        line
        for line in commit.message.decode().splitlines()
        if not line.startswith(("Config remote: ", "Config commit: "))
    ]


def _normalised(diff: str, name: str) -> str:
    return diff.replace(name, "<run>")
