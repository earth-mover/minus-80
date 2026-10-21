from minus80.core.icechunk_io import ensure_repo_access


class Client:
    def __init__(self):
        self.calls = []
        self.containers = {}

    def set_virtual_chunk_access_policy(self, org, **kwargs):
        self.calls.append(("vcap", org, kwargs))

    def update_virtual_chunk_containers(self, repo, *, add):
        self.calls.append(("vcc", repo))
        self.containers.update(add)

    def get_virtual_chunk_containers(self, repo):
        return self.containers


class OldClient(Client):
    """arraylake 1.2 and earlier: no update_virtual_chunk_containers."""
    update_virtual_chunk_containers = None

    def __getattribute__(self, name):
        if name == "update_virtual_chunk_containers":
            raise AttributeError(name)
        return super().__getattribute__(name)

    def authorize_virtual_chunk_access(self, repo, mapping):
        self.calls.append(("authorize", repo))
        self.containers.update(mapping)


def test_leaves_vcap_alone_by_default():
    client = Client()
    ensure_repo_access(client, "org/repo", container_url="s3://bucket/",
                       bucket_nickname="bucket")
    assert client.calls == [("vcc", "org/repo")]
    assert client.containers == {"s3://bucket/": "bucket"}


def test_sets_vcap_when_asked():
    client = Client()
    ensure_repo_access(client, "org/repo", container_url="s3://bucket/p/",
                       bucket_nickname="bucket", vcap_public=True)
    assert client.calls[0] == ("vcap", "org", {"bucket_nickname": "bucket",
                                               "subprefix": "", "public": True})


def test_old_client():
    client = OldClient()
    ensure_repo_access(client, "org/repo", container_url="s3://bucket/",
                       bucket_nickname="bucket")
    assert client.calls == [("authorize", "org/repo")]
    assert client.containers == {"s3://bucket/": "bucket"}
