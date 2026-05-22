# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
from dulwich import porcelain

from relcoord.config import IdcatSettings
from relcoord.git import (
    GitCredentialError,
    GithubRepo,
    clone_repository,
    github_https_url_from_ssh_style_uri,
    github_repo_from_url,
    installation_token_url,
    is_ssh_style_git_uri,
)


def test_clone_repository_uses_python_git_implementation(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    repo = porcelain.init(source)
    (source / "README.md").write_text("hello from dulwich\n")
    porcelain.add(repo, ["README.md"])
    commit = porcelain.commit(
        repo,
        message=b"initial",
        author=b"Relcoord <relcoord@example.com>",
        committer=b"Relcoord <relcoord@example.com>",
    ).decode("ascii")
    repo.close()

    result = clone_repository(str(source))

    assert result.source == str(source)
    assert result.head == commit
    assert (Path(result.path) / "README.md").read_text() == "hello from dulwich\n"


def test_clone_repository_uses_shallow_clone() -> None:
    repo = MagicMock()
    repo.head.return_value = b"abc123"

    with patch("relcoord.git.porcelain.clone", return_value=repo) as clone:
        result = clone_repository("https://example.com/acme/api.git")

    assert result.head == "abc123"
    assert clone.call_args.kwargs["depth"] == 1


def test_clone_repository_fetches_github_credentials_from_idcat(
    tmp_path: Path,
) -> None:
    token_file = tmp_path / "idcat-token"
    token_file.write_text("idcat-bearer-token\n")
    idcat = IdcatSettings(
        endpoint="https://idcat.example.test/base",
        github_app="deployments",
        token_path=token_file,
    )
    repo = MagicMock()
    repo.head.return_value = b"abc123"

    with (
        patch("relcoord.git.porcelain.clone", return_value=repo) as clone,
        patch("relcoord.git.httpx.post") as post,
    ):
        post.return_value = httpx.Response(
            200,
            text="github-installation-token\n",
            request=httpx.Request(
                "POST",
                "https://idcat.example.test/base/installation-token/deployments/acme/api",
            ),
        )

        clone_repository("https://github.com/acme/api.git", idcat=idcat)

    post.assert_called_once_with(
        "https://idcat.example.test/base/installation-token/deployments/acme/api",
        headers={"Authorization": "Bearer idcat-bearer-token"},
        timeout=10.0,
    )
    assert clone.call_args.kwargs["username"] == "x-access-token"
    assert clone.call_args.kwargs["password"] == "github-installation-token"


def test_clone_repository_reports_missing_idcat_token_file(tmp_path: Path) -> None:
    idcat = IdcatSettings(
        endpoint="https://idcat.example.test",
        github_app="deployments",
        token_path=tmp_path / "missing-token",
    )

    with pytest.raises(GitCredentialError, match="failed to read idcat token-path"):
        clone_repository("https://github.com/acme/api.git", idcat=idcat)


def test_clone_repository_reports_idcat_connection_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="relcoord.git")
    token_file = tmp_path / "idcat-token"
    token_file.write_text("idcat-bearer-token\n")
    idcat = IdcatSettings(
        endpoint="https://idcat.example.test/base",
        github_app="deployments",
        token_path=token_file,
    )

    with (
        patch(
            "relcoord.git.httpx.post",
            side_effect=httpx.ConnectError("Name or service not known"),
        ),
        pytest.raises(GitCredentialError) as exc_info,
    ):
        clone_repository("https://github.com/acme/api.git", idcat=idcat)

    assert (
        "failed to request installation token from idcat at "
        "https://idcat.example.test/base/installation-token/deployments/acme/api"
        in str(exc_info.value)
    )
    assert (
        "Failed to request installation token from idcat at "
        "https://idcat.example.test/base/installation-token/deployments/acme/api: "
        "Name or service not known" in caplog.text
    )


def test_github_repo_from_url_matches_idcat_helper_behavior() -> None:
    assert github_repo_from_url("https://github.com/acme/api.git/info/refs") == (
        GithubRepo(owner="acme", name="api")
    )
    assert github_repo_from_url("ssh://github.com/acme/api.git") is None
    assert github_repo_from_url("https://example.com/acme/api.git") is None


def test_github_https_url_from_ssh_style_uri_converts_github_repos() -> None:
    assert (
        github_https_url_from_ssh_style_uri("git@github.com:acme/api.git")
        == "https://github.com/acme/api.git"
    )
    assert (
        github_https_url_from_ssh_style_uri("ssh://git@github.com/acme/api")
        == "https://github.com/acme/api.git"
    )
    assert (
        github_https_url_from_ssh_style_uri("git+ssh://git@github.com/acme/api.git")
        == "https://github.com/acme/api.git"
    )
    assert (
        github_https_url_from_ssh_style_uri("git@gitlab.example.com:acme/api.git")
        is None
    )
    assert (
        github_https_url_from_ssh_style_uri("https://github.com/acme/api.git") is None
    )


def test_is_ssh_style_git_uri_recognizes_supported_forms() -> None:
    assert is_ssh_style_git_uri("git@github.com:acme/api.git") is True
    assert is_ssh_style_git_uri("ssh://git@github.com/acme/api.git") is True
    assert is_ssh_style_git_uri("git+ssh://git@github.com/acme/api.git") is True
    assert is_ssh_style_git_uri("https://github.com/acme/api.git") is False


def test_installation_token_url_preserves_idcat_base_path(tmp_path: Path) -> None:
    assert (
        installation_token_url(
            IdcatSettings(
                endpoint="https://idcat.example.test/base/",
                github_app="deployments",
                token_path=tmp_path / "idcat-token",
            ),
            GithubRepo(owner="acme", name="api"),
        )
        == "https://idcat.example.test/base/installation-token/deployments/acme/api"
    )
