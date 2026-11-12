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
from collections.abc import Iterable
from typing import NamedTuple

from sources import SAMPLES

#: The sample that is set up already: a new sample must use the same bucket.
REFERENCE = "kidney"


class SetupError(ValueError):
    """A fault that the script reports itself: its text holds no name or location of the service."""


class Namespace(NamedTuple):
    """Where a namespace lives; the script prints none of these fields."""

    bucket: str
    bucket_id: str
    url: str
    prefix: str


def split_location(bucket_url: str, prefix: str) -> tuple[str, str]:
    """The location and the sub-prefix of a namespace; raises ``ValueError`` when the prefix names no directory."""
    subprefix = prefix.strip("/")
    if not any(subprefix.split("/")):
        raise ValueError("the namespace has no sub-prefix of its own")
    return f"{bucket_url.rstrip('/')}/{subprefix}/", subprefix


def policy_state(policies: Iterable[tuple[str, str, bool]], bucket_id: str, subprefix: str) -> str:
    """``public``, ``wider``, ``private`` or ``absent`` for the policies ``(bucket_id, subprefix, public)`` of an org.

    Sub-prefixes compare without leading and trailing slashes, and by whole segments.
    A policy is ``wider`` when its sub-prefix is empty or an ancestor of ``subprefix``.
    """
    exact, wider = [], False
    for policy_bucket, policy_prefix, public in policies:
        if policy_bucket != bucket_id:
            continue
        policy_prefix = policy_prefix.strip("/")
        if policy_prefix == subprefix:
            exact.append(public)
        elif not policy_prefix or subprefix.startswith(f"{policy_prefix}/"):
            wider = True
    if True in exact:
        return "public"
    if wider:
        return "wider"
    return "private" if exact else "absent"


class Org:
    """The calls to one Arraylake org. The client opens on the first call."""

    def __init__(self, org: str, client=None):
        self.org, self._client = org, client

    @property
    def client(self):
        if self._client is None:
            from arraylake import Client

            self._client = Client()
        return self._client

    def default_bucket(self) -> str:
        """The nickname of the org's default bucket."""
        for bucket in self.client.list_bucket_configs(self.org):
            if bucket.is_default:
                return bucket.nickname
        raise SetupError("the org has no default bucket")

    def repo_exists(self, name: str) -> bool:
        return any(repo.name == name for repo in self.client.list_repos(self.org))

    def repo_bucket(self, name: str) -> str | None:
        """The nickname of the bucket of an existing repo, or None when the service shows none."""
        repo = next(r for r in self.client.list_repos(self.org) if r.name == name)
        return repo.bucket.nickname if repo.bucket else None

    def create_repo(self, name: str) -> None:
        self.client.create_repo(f"{self.org}/{name}")

    def namespace_location(self, namespace: str) -> Namespace | None:
        """The storage of the namespace, or None when the namespace is absent."""
        found = next((n for n in self.client.list_iceberg_namespaces(self.org) if n.name == namespace), None)
        if found is None:
            return None
        bucket = next((b for b in self.client.list_bucket_configs(self.org) if b.nickname == found.bucket_nickname), None)
        if bucket is None:
            raise SetupError("the bucket of the namespace is not in the org's bucket list")
        return Namespace(bucket.nickname, str(bucket.id), bucket.url, found.prefix)

    def create_namespace(self, namespace: str) -> None:
        self.client.create_iceberg_namespace(self.org, namespace)

    def container(self, repo: str) -> str | None:
        """The URL prefix of the repo's one virtual chunk container, or None when it has none."""
        prefixes = list(self.client.get_virtual_chunk_containers(f"{self.org}/{repo}"))
        if len(prefixes) > 1:
            raise SetupError(f"the repo has {len(prefixes)} virtual chunk containers; the script handles one")
        return prefixes[0] if prefixes else None

    def set_container(self, repo: str, location: str, bucket: str) -> None:
        self.client.update_virtual_chunk_containers(f"{self.org}/{repo}", add={location: bucket})

    def policy(self, bucket_id: str, subprefix: str) -> str:
        """The state of the org's access policies for ``subprefix``; see ``policy_state``."""
        policies = self.client.list_virtual_chunk_access_policies(self.org)
        return policy_state([(p.bucket_id, p.subprefix, p.public) for p in policies], bucket_id, subprefix)

    def set_policy(self, bucket: str, subprefix: str) -> None:
        self.client.set_virtual_chunk_access_policy(self.org, bucket_nickname=bucket, subprefix=subprefix, public=False)


def _check_bucket(org: Org, repo: str, namespace: str, own: Namespace | None) -> str | None:
    """A reason that the sample's bucket is not the reference sample's, or None."""
    reference = org.namespace_location(f"{SAMPLES[REFERENCE].repo}-tables")
    if reference is None or reference.bucket != org.default_bucket():
        return "the default bucket is not the bucket of the reference sample"
    if org.repo_exists(repo) and org.repo_bucket(repo) != reference.bucket:
        return "the repo is not on the bucket of the reference sample"
    if own is not None and own.bucket != reference.bucket:
        return "the namespace is not on the bucket of the reference sample"
    return None


def plan(sample_name: str, org: Org, *, dry_run: bool) -> tuple[list[str], bool]:
    """Check and, unless ``dry_run``, create the pieces in order; the printed lines and whether all went well.

    A dry run calls only the reading methods. Each check runs before the write that it guards.
    """
    repo = SAMPLES[sample_name].repo
    namespace = f"{repo}-tables"
    lines, piece = [], "bucket"

    def done(label: str, status: str) -> None:
        lines.append(f"{label}: {status}")

    def create(label: str, make, *args) -> None:
        if dry_run:
            return done(label, "would create")
        make(*args)
        done(label, "created")

    try:
        own = org.namespace_location(namespace)
        if reason := _check_bucket(org, repo, namespace, own):
            done("bucket", f"DIFFERS ({reason})")
            return lines, False
        done("bucket", "as the reference sample")

        piece = label = f"repo {repo}"
        repo_present = org.repo_exists(repo)
        if repo_present:
            done(label, "exists")
        else:
            create(label, org.create_repo, repo)

        piece = label = f"namespace {namespace}"
        status = "exists"
        if own is None and dry_run:
            status = "would create"
        elif own is None:
            org.create_namespace(namespace)
            own, status = org.namespace_location(namespace), "created"
            if own is None:
                raise SetupError("the namespace does not show after its creation")
        where = None
        if own is not None:
            try:
                where = split_location(own.url, own.prefix)
            except ValueError:
                done(label, "DIFFERS (the namespace has no sub-prefix of its own)")
                return lines, False
        done(label, status)

        piece = label = "container"
        held = org.container(repo) if repo_present else None
        if where is None:
            if held is None:
                done(label, "would create")
            else:
                done(label, "DIFFERS (the repo has a container for another location)")
                return lines, False
        else:
            location, subprefix = where
            if held is None:
                create(label, org.set_container, repo, location, own.bucket)
            elif held == location:
                done(label, "exists")
            else:
                done(label, "DIFFERS (the repo has a container for another location)")
                return lines, False

        piece = label = "policy"
        if where is None:
            done(label, "would create")
            return lines, True
        state = org.policy(own.bucket_id, subprefix)
        if state == "private":
            done(label, "exists")
        elif state == "public":
            done(label, "DIFFERS (a public policy covers the location)")
        elif state == "wider":
            done(label, "DIFFERS (a policy for a wider prefix covers the location)")
        else:
            create(label, org.set_policy, own.bucket, subprefix)
        return lines, state in ("private", "absent")
    except Exception as error:
        # Only the script's own text is safe to print: the service's text can hold a bucket name or a prefix.
        done(piece, f"ERROR ({_describe(error)})")
        return lines, False


def _describe(error: Exception) -> str:
    return f"{type(error).__name__}: {error}" if isinstance(error, SetupError) else type(error).__name__


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
