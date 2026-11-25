# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
"""Generating the outputs of one manifests repository side by side.

Outputs sharing a manifests repository share one checkout, and each output's
generation commits on top of the one before it. That is what makes them run one
after another, and it is the order the repository's history and diff follow.

With ``max-output-concurrency`` above 1, a batch of such outputs is generated at
once instead, each output by a worker process of its own into a scratch
repository of its own:

- The scratch repository borrows the shared checkout's objects through git
  alternates and checks out only the output's directory (a cone-mode sparse
  checkout), at the commit the shared checkout stands at when the batch starts.
  Every output of a batch therefore generates from the same base commit, and
  nothing a worker writes reaches the shared checkout or another worker.
- Once every output of the batch has generated, what each committed in its
  scratch repository is committed onto the shared checkout in configured order:
  the output's directory as the worker committed it, with the worker's commit
  message and author. manifest-builder only ever commits inside the output's
  directory, and reads only that directory of the checkout, so with directories
  that do not overlap the history, the trees and the diff are the ones
  generating one after another produces.

Processes rather than threads, because the per-output work is Python that holds
the GIL, and because manifest-builder keeps per-process state a concurrent
generation in the same process could race on: the plugin modules it re-imports
for every run, and the resource scopes those plugins declare, which a run clears
and refills.

Outputs whose directories overlap (one inside the other, the same directory, or
the repository root) are generated one after another as before, as is a batch of
a single output, since there is nothing to run beside it.
"""

from __future__ import annotations

import contextvars
import logging
import multiprocessing
import os
import pickle
import shutil
import stat
import tempfile
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ProcessPoolExecutor, ThreadPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from functools import partial
from pathlib import Path, PurePosixPath
from typing import Any, Protocol, cast

from dulwich import porcelain
from dulwich.diff_tree import tree_changes
from dulwich.errors import NotGitRepository
from dulwich.index import update_working_tree
from dulwich.object_store import BaseObjectStore
from dulwich.objects import Commit, ObjectID, Tree
from dulwich.refs import HEADREF
from dulwich.repo import Repo
from manifest_builder import GenerationResult

logger = logging.getLogger(__name__)

_GITLINK_MODE = 0o160000


class _Output(Protocol):
    """What a batch needs of an output; relcoord's OutputSettings provides it."""

    @property
    def name(self) -> str: ...

    @property
    def directory(self) -> Path: ...


class OutputGenerationError(RuntimeError):
    """A worker's generation failure, carried back by its message."""


class OutputWorkerError(RuntimeError):
    """Raised when a worker process ended before its generation did.

    The kernel killing a process that ran out of memory is the likely cause.
    Only that worker's batch fails, rather than all of relcoord with it.
    """


class OutputWorkers:
    """How many outputs may be generating in worker processes at any moment.

    One instance is shared by every change and diff relcoord serves, so the
    limit holds however many requests and repositories are being generated at
    once: a generation waits for a slot before it starts, and the memory and CPU
    relcoord needs grow with this number rather than with the traffic.
    """

    def __init__(self, limit: int) -> None:
        if limit < 1:
            raise ValueError("an output worker limit must be at least 1")
        self.limit = limit
        self._slots = threading.BoundedSemaphore(limit)

    def acquire(self) -> None:
        self._slots.acquire()

    def release(self) -> None:
        self._slots.release()


@dataclass(frozen=True)
class Generation:
    """Where one output is generated, and the generate call that does it.

    ``root`` is the checkout the output's directory is generated into: the
    shared checkout when outputs generate one after another, the output's own
    scratch repository otherwise. ``generate`` takes manifest-builder's
    ``generate`` arguments, with the output path inside ``root``.
    """

    root: Path
    generate: Callable[..., GenerationResult]


def generate_batch[O: _Output, T](
    outputs: Sequence[O],
    checkout: Path,
    generate_one: Callable[[O, Generation], T],
    *,
    workers: OutputWorkers | None,
    config: Path,
    local_generate: Callable[..., GenerationResult],
    repository: str,
    until: Callable[[], bool] | None = None,
) -> list[tuple[O, T]]:
    """Generate ``outputs`` into ``checkout``, returning each one's result in order.

    ``generate_one`` does everything one output's generation involves, given
    where to generate it. ``until`` is asked before each output starts and stops
    the batch once it answers true; the outputs started by then are finished and
    kept, and since outputs start in configured order they are always the first
    few of it.

    A failure stops further outputs from starting, lets those already started
    finish, since a generation cannot be interrupted part way, and is raised
    once they have: the first failure in configured order, which is the one
    generating one after another would have raised. Nothing of a failed batch is
    committed to ``checkout``.
    """
    serial = Generation(root=checkout, generate=local_generate)
    if workers is None or workers.limit <= 1 or len(outputs) <= 1:
        return _serially(outputs, serial, generate_one, until)
    unsafe = _overlap(outputs) or _not_a_directory(outputs, checkout)
    if unsafe is not None:
        logger.info(
            "generating the outputs of %s one after another: %s",
            repository,
            unsafe,
        )
        return _serially(outputs, serial, generate_one, until)
    return _concurrently(
        outputs,
        checkout,
        generate_one,
        workers=workers,
        config=config,
        repository=repository,
        until=until,
    )


def submit_in_context[T](pool: ThreadPoolExecutor, task: Callable[[], T]) -> Future[T]:
    """Run a task in a pool inside a copy of the caller's context.

    Pool threads do not inherit context variables, and the current trace span
    is one, so without the copy a task's spans would start traces of their own
    rather than nest under the request they belong to.
    """
    context = contextvars.copy_context()
    return pool.submit(lambda: context.run(task))


def _serially[O: _Output, T](
    outputs: Sequence[O],
    generation: Generation,
    generate_one: Callable[[O, Generation], T],
    until: Callable[[], bool] | None,
) -> list[tuple[O, T]]:
    results: list[tuple[O, T]] = []
    for output in outputs:
        if until is not None and until():
            break
        results.append((output, generate_one(output, generation)))
    return results


def _concurrently[O: _Output, T](
    outputs: Sequence[O],
    checkout: Path,
    generate_one: Callable[[O, Generation], T],
    *,
    workers: OutputWorkers,
    config: Path,
    repository: str,
    until: Callable[[], bool] | None,
) -> list[tuple[O, T]]:
    base = _head(checkout)
    _refresh_index(config)
    width = min(workers.limit, len(outputs))
    logger.info(
        "generating %d outputs of %s from %s, up to %d at once",
        len(outputs),
        repository,
        base,
        width,
    )
    scratch = Path(tempfile.mkdtemp(prefix="outputs-", dir=checkout.parent))
    failed = threading.Event()
    admitted: list[tuple[O, Path, Future[T]]] = []
    try:
        with (
            ProcessPoolExecutor(
                max_workers=width,
                mp_context=_worker_context(),
                # A worker generates one output after another, as relcoord's
                # own process does without workers, and the pool goes with the
                # batch, so its memory is handed back once the batch is done.
                initializer=_configure_worker_logging,
                initargs=_worker_logging(),
            ) as processes,
            ThreadPoolExecutor(
                max_workers=width, thread_name_prefix="relcoord-output"
            ) as threads,
        ):
            # Outputs are admitted here, one at a time and in configured order,
            # so the ones a stop leaves out are always the last.
            for index, output in enumerate(outputs):
                workers.acquire()
                if failed.is_set() or (until is not None and until()):
                    workers.release()
                    break
                root = scratch / str(index)
                generation = Generation(
                    root=root,
                    generate=partial(
                        _generate_in_worker,
                        processes,
                        output.name,
                        checkout,
                        base,
                        root,
                        _directory(output),
                    ),
                )
                try:
                    future = submit_in_context(
                        threads,
                        partial(
                            _run_admitted,
                            generate_one,
                            output,
                            generation,
                            workers,
                            failed,
                        ),
                    )
                except BaseException:
                    # The slot is the task's to give back once it runs.
                    workers.release()
                    raise
                admitted.append((output, root, future))
        for _, _, future in admitted:
            # Raises the first failure in configured order.
            future.result()
        integrated = False
        for output, root, _ in admitted:
            integrated |= _integrate(checkout, root, base, output)
        if integrated:
            _check_out_head(checkout, base)
        return [(output, future.result()) for output, _, future in admitted]
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _run_admitted[O: _Output, T](
    generate_one: Callable[[O, Generation], T],
    output: O,
    generation: Generation,
    workers: OutputWorkers,
    failed: threading.Event,
) -> T:
    try:
        return generate_one(output, generation)
    except BaseException:
        failed.set()
        raise
    finally:
        workers.release()


def _generate_in_worker(
    processes: ProcessPoolExecutor,
    name: str,
    checkout: Path,
    base: str,
    root: Path,
    directory: str,
    *args: Any,
    **kwargs: Any,
) -> GenerationResult:
    """Generate in a worker process, waiting for it on the calling thread."""
    future = processes.submit(
        _generate_in_scratch, checkout, base, root, directory, args, kwargs
    )
    try:
        return future.result()
    except BrokenProcessPool as exc:
        raise OutputWorkerError(
            f"a worker process ended before generating {name} finished, or one "
            "generating alongside it did; the likely cause is the process being "
            "killed for running out of memory"
        ) from exc


def _generate_in_scratch(
    checkout: Path,
    base: str,
    root: Path,
    directory: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> GenerationResult:
    """Worker process entry point: create the scratch repository, then generate."""
    from manifest_builder import generate

    try:
        create_scratch(checkout, base, root, directory)
        return generate(*args, **kwargs)
    except Exception as exc:
        raise _transportable(exc) from exc


def _transportable(exc: Exception) -> Exception:
    """``exc``, or a stand-in with its message where it would not reach the parent.

    A failure travels back from a worker pickled, and an exception whose
    constructor takes other arguments than it passes on, as manifest-builder's
    ManifestError does, fails to unpickle. The pool would then call itself broken
    and fail every output with it. relcoord reports a generation's failure by its
    message, which the stand-in carries unchanged.
    """
    try:
        pickle.loads(pickle.dumps(exc))
    except Exception:
        # Anything a constructor or __reduce__ raises means the same thing.
        logger.debug(
            "%s does not survive pickling; sending back its message",
            type(exc).__name__,
            exc_info=True,
        )
        return OutputGenerationError(str(exc))
    return exc


def create_scratch(checkout: Path, base: str, root: Path, directory: str) -> None:
    """Make ``root`` a repository at ``base`` with only ``directory`` checked out.

    Objects come from ``checkout`` through git alternates, so nothing is copied
    but the checkout's config and the files of ``directory``; what the scratch
    repository commits goes into objects of its own. Everything outside
    ``directory`` stays in the index, marked skip-worktree, so a commit made
    here carries the rest of the tree unchanged. The repository root's own
    files, ``.gitignore`` and ``.gitattributes`` among them, are checked out as
    a cone-mode sparse checkout always does, and so are those of each directory
    on the way to ``directory``.
    """
    shared = Repo(str(checkout))
    try:
        (_, branch), _ = shared.refs.follow(HEADREF)
        controldir = Path(shared.controldir())
        objects = Path(shared.object_store.path).resolve()
    finally:
        shared.close()
    if branch is None:
        raise RuntimeError(f"{checkout} is not on a branch")
    root.mkdir(parents=True, exist_ok=True)
    repo = Repo.init(str(root))
    try:
        scratch_controldir = Path(repo.controldir())
        shutil.copyfile(controldir / "config", scratch_controldir / "config")
        if (controldir / "shallow").exists():
            shutil.copyfile(controldir / "shallow", scratch_controldir / "shallow")
        repo.object_store.add_alternate_path(str(objects))
        repo.refs.set_symbolic_ref(HEADREF, branch)
        repo.refs[branch] = ObjectID(base.encode("ascii"))
        parts = PurePosixPath(directory).parts
        if parts:
            config = repo.get_config()
            config.set((b"core",), b"sparseCheckout", True)
            config.set((b"core",), b"sparseCheckoutCone", True)
            config.write_to_path()
            info = scratch_controldir / "info"
            info.mkdir(exist_ok=True)
            (info / "sparse-checkout").write_text(_cone_patterns(parts))
    finally:
        repo.close()
    porcelain.reset(str(root), "hard", base)


def _cone_patterns(parts: Sequence[str]) -> str:
    """The sparse-checkout patterns ``git sparse-checkout set --cone`` writes."""
    lines = ["/*", "!/*/"]
    for depth in range(1, len(parts)):
        parent = "/".join(parts[:depth])
        lines += [f"/{parent}/", f"!/{parent}/*/"]
    lines.append(f"/{'/'.join(parts)}/")
    return "\n".join(lines) + "\n"


def _integrate(checkout: Path, scratch: Path, base: str, output: _Output) -> bool:
    """Commit onto ``checkout`` what ``output`` committed in ``scratch``.

    The commit carries the checkout's tree with the output's directory replaced
    by the one the worker committed, and the worker's message, author and
    committer, on top of whatever the checkout's branch holds by now. Returns
    whether there was anything to commit. Only the branch moves here; the caller
    brings the index and the files along once the batch is done.
    """
    directory = _directory(output)
    parts = PurePosixPath(directory).parts
    generated_repo = Repo(str(scratch))
    shared = Repo(str(checkout))
    try:
        head = generated_repo.head()
        if head.decode("ascii") == base:
            return False
        generated = generated_repo[head]
        if not isinstance(generated, Commit) or [
            parent.decode("ascii") for parent in generated.parents
        ] != [base]:
            raise RuntimeError(
                f"generating {output.name} left something other than one commit "
                f"on top of {base}"
            )
        base_tree = cast(Commit, generated_repo[ObjectID(base.encode("ascii"))]).tree
        outside = sorted(
            path.decode("utf-8", errors="replace")
            for change in tree_changes(
                generated_repo.object_store, base_tree, generated.tree
            )
            for path in _change_paths(change)
            if not _inside(path, parts)
        )
        if outside:
            raise RuntimeError(
                f"generating {output.name} committed outside its directory "
                f"{directory}: {', '.join(outside[:3])}"
            )
        subtree = _subtree(generated_repo.object_store, generated.tree, parts)
        if subtree is not None:
            _copy_objects(generated_repo.object_store, shared.object_store, subtree)
        parent = shared.head()
        commit = Commit()
        commit.tree = _with_subtree(
            shared.object_store,
            cast(Commit, shared[parent]).tree,
            parts,
            subtree,
        )
        commit.parents = [parent]
        commit.author = generated.author
        commit.author_time = generated.author_time
        commit.author_timezone = generated.author_timezone
        commit.committer = generated.committer
        commit.commit_time = generated.commit_time
        commit.commit_timezone = generated.commit_timezone
        commit.encoding = generated.encoding
        commit.message = generated.message
        shared.object_store.add_object(commit)
        if not shared.refs.set_if_equals(HEADREF, parent, commit.id):
            raise RuntimeError(f"{checkout} moved while {output.name} was committed")
        return True
    finally:
        generated_repo.close()
        shared.close()


def _check_out_head(checkout: Path, base: str) -> None:
    """Bring the index and the files of ``checkout`` from ``base`` to its HEAD.

    Only the paths the batch changed are written, rather than every file being
    compared as a hard reset would; the checkout matched ``base`` when the batch
    started, and nothing but this module has written to it since.
    """
    repo = Repo(str(checkout))
    try:
        old = cast(Commit, repo[ObjectID(base.encode("ascii"))]).tree
        new = cast(Commit, repo[repo.head()]).tree
        update_working_tree(
            repo,
            old,
            new,
            change_iterator=tree_changes(repo.object_store, old, new),
            blob_normalizer=repo.get_blob_normalizer(),
        )
    finally:
        repo.close()


def _change_paths(change: Any) -> list[bytes]:
    return [
        entry.path
        for entry in (change.old, change.new)
        if entry is not None and entry.path is not None
    ]


def _inside(path: bytes, parts: Sequence[str]) -> bool:
    prefix = "/".join(parts).encode("utf-8")
    return not prefix or path == prefix or path.startswith(prefix + b"/")


def _subtree(
    store: BaseObjectStore, tree: ObjectID, parts: Sequence[str]
) -> ObjectID | None:
    """The tree at ``parts`` below ``tree``, or None where there is none."""
    current = tree
    for part in parts:
        entries = cast(Tree, store[current])
        try:
            mode, sha = entries[part.encode("utf-8")]
        except KeyError:
            return None
        if not stat.S_ISDIR(mode):
            return None
        current = sha
    return current


def _with_subtree(
    store: BaseObjectStore,
    tree: ObjectID | None,
    parts: Sequence[str],
    subtree: ObjectID | None,
) -> ObjectID:
    """``tree`` with the tree at ``parts`` replaced by ``subtree``, None removing it."""
    replaced = _replace(store, tree, parts, subtree)
    if replaced is not None:
        return replaced
    empty = Tree()
    store.add_object(empty)
    return empty.id


def _replace(
    store: BaseObjectStore,
    tree: ObjectID | None,
    parts: Sequence[str],
    subtree: ObjectID | None,
) -> ObjectID | None:
    if not parts:
        return subtree
    existing = cast(Tree, store[tree]) if tree is not None else Tree()
    name = parts[0].encode("utf-8")
    below: ObjectID | None = None
    if name in existing:
        mode, sha = existing[name]
        if stat.S_ISDIR(mode):
            below = sha
    child = _replace(store, below, parts[1:], subtree)
    updated = Tree()
    for entry in existing.items():
        if entry.path != name:
            updated.add(entry.path, entry.mode, entry.sha)
    if child is not None:
        updated.add(name, stat.S_IFDIR, child)
    if not len(updated):
        # git stores no empty trees; the directory goes with its last file.
        return None
    store.add_object(updated)
    return updated.id


def _copy_objects(
    source: BaseObjectStore, target: BaseObjectStore, tree: ObjectID
) -> None:
    """Copy into ``target`` whatever of ``tree`` it does not have yet.

    A tree ``target`` has already comes with everything below it, so only the
    directories the generation changed are walked.
    """
    pending = [tree]
    while pending:
        sha = pending.pop()
        if sha in target:
            continue
        obj = source[sha]
        target.add_object(obj)
        if isinstance(obj, Tree):
            pending.extend(
                entry.sha
                for entry in obj.items()
                # A gitlink names a commit of another repository.
                if entry.mode != _GITLINK_MODE
            )


def _overlap(outputs: Sequence[_Output]) -> str | None:
    """Say which two outputs write overlapping directories, if any do."""
    parts = [(output, PurePosixPath(_directory(output)).parts) for output in outputs]
    for index, (first, first_parts) in enumerate(parts):
        for second, second_parts in parts[index + 1 :]:
            shorter = min(len(first_parts), len(second_parts))
            if first_parts[:shorter] == second_parts[:shorter]:
                return (
                    f"{first.name} ({_directory(first) or 'the repository root'}) "
                    f"and {second.name} "
                    f"({_directory(second) or 'the repository root'}) write "
                    "overlapping directories"
                )
    return None


def _not_a_directory(outputs: Sequence[_Output], checkout: Path) -> str | None:
    """Say which output's directory, or a directory on the way to it, is not one.

    A symlink would let two outputs whose names differ write the same files,
    which a scratch repository checking out the one directory cannot follow.
    """
    repo = Repo(str(checkout))
    try:
        tree = cast(Commit, repo[repo.head()]).tree
        for output in outputs:
            current = tree
            for part in PurePosixPath(_directory(output)).parts:
                entries = cast(Tree, repo[current])
                try:
                    mode, sha = entries[part.encode("utf-8")]
                except KeyError:
                    break
                if not stat.S_ISDIR(mode):
                    return (
                        f"{output.name}'s directory {_directory(output)} goes "
                        f"through {part}, which is not a directory"
                    )
                current = sha
    finally:
        repo.close()
    return None


def _directory(output: _Output) -> str:
    """The output's directory as a tree path, empty for the repository root."""
    directory = os.path.normpath(output.directory.as_posix())
    return "" if directory == "." else directory


def _head(checkout: Path) -> str:
    repo = Repo(str(checkout))
    try:
        return repo.head().decode("ascii")
    finally:
        repo.close()


# Serializes refreshing an index, which takes index.lock: two batches starting
# together would otherwise have one of them fail to take it.
_INDEX_REFRESH_LOCK = threading.Lock()


def _refresh_index(config: Path) -> None:
    """Bring the config checkout's index up to date before workers read it.

    manifest-builder asks whether the config checkout is dirty before it
    commits, and dulwich answers by refreshing any stale stat information it
    finds in the index and writing the index back, which takes index.lock.
    Workers asking together could then fail to take the lock. Refreshing it
    once here leaves them nothing to write.
    """
    try:
        repo = Repo.discover(str(config))
    except NotGitRepository:
        return
    try:
        with _INDEX_REFRESH_LOCK:
            porcelain.status(repo)
    finally:
        repo.close()


def _worker_context() -> multiprocessing.context.BaseContext:
    """forkserver, with this module and what it imports loaded in the server.

    A worker is forked from a server process started for the purpose, which is
    single threaded, rather than from relcoord itself, which runs threads a fork
    would copy in whatever state they are in. Preloading means a worker starts
    with manifest-builder and dulwich already imported, and with the main module,
    which multiprocessing would otherwise import again in every worker; relcoord's
    entry points only start the service under ``if __name__ == "__main__"``.
    The server starts with the environment relcoord had when the first batch ran.
    """
    context = multiprocessing.get_context("forkserver")
    context.set_forkserver_preload(["__main__", __name__])
    return context


def _worker_logging() -> tuple[int, str | None, str | None]:
    """The parent's log level and format, for a worker to log the same way."""
    root = logging.getLogger()
    formatter = next(
        (handler.formatter for handler in root.handlers if handler.formatter), None
    )
    if formatter is None:
        return root.getEffectiveLevel(), None, None
    return root.getEffectiveLevel(), formatter._fmt, formatter.datefmt


def _configure_worker_logging(level: int, fmt: str | None, datefmt: str | None) -> None:
    if fmt is None:
        logging.basicConfig(level=level, force=True)
    else:
        logging.basicConfig(level=level, format=fmt, datefmt=datefmt, force=True)
