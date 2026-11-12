"""Set up the Arraylake repo, tables namespace, virtual chunk container and access policy of one sample.

    setup_sample.py --sample brain --org <org> --dry-run
    setup_sample.py --sample brain --org <org>

Each piece prints one line: ``exists``, ``created``, ``would create`` or ``DIFFERS (<what>)``.
A piece that exists as wanted is skipped. A piece that differs stops the script with exit 1.
The namespace's storage location stays in memory and never appears in the output.
Needs a login (``al auth login``) with the right to create repos and to set an access policy.
"""

from __future__ import annotations

import argparse
import sys

from facts import error_text
from sources import SAMPLES


class Org:
    """The calls to one Arraylake org. The client opens on the first call."""

    def __init__(self, org: str):
        self.org, self._client = org, None

    @property
    def client(self):
        if self._client is None:
            from arraylake import Client

            self._client = Client()
        return self._client

    def _bucket(self, location: str):
        """The org's bucket config that holds ``location``."""
        return next(b for b in self.client.list_bucket_configs(self.org) if location.startswith(b.url))

    def repo_exists(self, name: str) -> bool:
        return any(repo.name == name for repo in self.client.list_repos(self.org))

    def create_repo(self, name: str) -> None:
        self.client.create_repo(f"{self.org}/{name}")

    def namespace_location(self, namespace: str) -> str | None:
        """The URL of the namespace's storage, or None when the namespace is absent."""
        found = next((n for n in self.client.list_iceberg_namespaces(self.org) if n.name == namespace), None)
        if found is None:
            return None
        return self.client.get_bucket_config(org=self.org, nickname=found.bucket_nickname).url_with_subprefix(found.prefix)

    def create_namespace(self, namespace: str) -> None:
        self.client.create_iceberg_namespace(self.org, namespace)

    def container(self, repo: str) -> str | None:
        """The URL prefix of the repo's one virtual chunk container, or None when it has none."""
        prefixes = list(self.client.get_virtual_chunk_containers(f"{self.org}/{repo}"))
        if len(prefixes) > 1:
            raise ValueError(f"the repo has {len(prefixes)} virtual chunk containers; the script handles one")
        return prefixes[0] if prefixes else None

    def set_container(self, repo: str, location: str) -> None:
        self.client.update_virtual_chunk_containers(
            f"{self.org}/{repo}", add={location: self._bucket(location).nickname})

    def _subprefix(self, location: str) -> str:
        return location.removeprefix(self._bucket(location).url).strip("/")

    def policy(self, location: str) -> bool:
        """Whether a private access policy covers ``location``."""
        bucket_id, subprefix = str(self._bucket(location).id), self._subprefix(location)
        return any(p.bucket_id == bucket_id and p.subprefix.strip("/") == subprefix and not p.public
                   for p in self.client.list_virtual_chunk_access_policies(self.org))

    def set_policy(self, location: str) -> None:
        self.client.set_virtual_chunk_access_policy(
            self.org, bucket_nickname=self._bucket(location).nickname, subprefix=self._subprefix(location), public=False)


def _same(a: str, b: str) -> bool:
    return a.rstrip("/") == b.rstrip("/")


def plan(sample_name: str, org: Org, *, dry_run: bool) -> tuple[list[str], bool]:
    """Check and, unless ``dry_run``, create the four pieces in order; the printed lines and whether all went well.

    A dry run calls only the reading methods.
    """
    repo = SAMPLES[sample_name].repo
    namespace = f"{repo}-tables"
    lines, piece = [], "repo"

    def done(label: str, status: str) -> None:
        lines.append(f"{label}: {status}")

    def create(label: str, make, *args) -> None:
        if dry_run:
            return done(label, "would create")
        make(*args)
        done(label, "created")

    try:
        label = f"repo {repo}"
        repo_present = org.repo_exists(repo)
        if repo_present:
            done(label, "exists")
        else:
            create(label, org.create_repo, repo)

        piece = label = f"namespace {namespace}"
        location = org.namespace_location(namespace)
        if location is not None:
            done(label, "exists")
        else:
            create(label, org.create_namespace, namespace)
            location = None if dry_run else org.namespace_location(namespace)

        piece = label = "container"
        held = org.container(repo) if repo_present else None
        if location is None:
            done(label, "would create")
        elif held is None:
            create(label, org.set_container, repo, location)
        elif _same(held, location):
            done(label, "exists")
        else:
            done(label, "DIFFERS (the repo has a container for another location)")
            return lines, False

        piece = label = "policy"
        if location is None:
            done(label, "would create")
        elif org.policy(location):
            done(label, "exists")
        else:
            create(label, org.set_policy, location)
    except Exception as error:
        done(piece, f"ERROR ({error_text(error)})")
        return lines, False
    return lines, True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sample", required=True, choices=SAMPLES)
    parser.add_argument("--org", required=True)
    parser.add_argument("--dry-run", action="store_true", help="read the org and create nothing")
    args = parser.parse_args(argv)
    lines, ok = plan(args.sample, Org(args.org), dry_run=args.dry_run)
    print("\n".join(lines), flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
