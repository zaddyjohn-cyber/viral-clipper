"""
Where rendered clips live.

Local disk by default, so nothing new has to be set up to start. The S3
backend speaks to anything S3-shaped (S3, R2, B2, Spaces, MinIO) and is what
makes posting possible later: Instagram will not accept a reel from bytes, it
fetches a URL itself, so the render has to sit somewhere public first.

Both backends answer the same four questions: put it, give me its URL, give me
its bytes, delete it.
"""

import mimetypes
import os
import re
import shutil
from pathlib import Path

import settings


def safe_key(*parts):
    """Build a storage key that is legal on every backend and on Windows."""
    out = []
    for p in parts:
        p = re.sub(r"[^A-Za-z0-9._-]+", "-", str(p)).strip("-.")
        if p:
            out.append(p[:120])
    return "/".join(out)


def clip_key(job_id, n, filename):
    return safe_key("jobs", job_id, f"{n:02d}-{filename}")


def guess_type(path):
    return mimetypes.guess_type(str(path))[0] or "application/octet-stream"


class LocalStorage:
    """Files under one directory, served back through the API."""

    name = "local"
    public = False          # served by us, so not reachable by a third party

    def __init__(self, root=None):
        self.root = Path(root or settings.STORAGE_DIR)

    def _path(self, key):
        p = (self.root / key).resolve()
        root = self.root.resolve()
        # A key is never user-typed today, but path traversal here would hand
        # out any file on the box, so refuse anything that escapes the root.
        if root not in p.parents and p != root:
            raise ValueError("key escapes the storage root")
        return p

    def put(self, key, src, content_type=None, move=False):
        dest = self._path(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if move:
            shutil.move(str(src), str(dest))
        else:
            shutil.copy2(str(src), str(dest))
        return key

    def url(self, key):
        return "/api/media/" + key

    def local_path(self, key):
        p = self._path(key)
        return p if p.exists() else None

    def read(self, key):
        return self._path(key).read_bytes()

    def size(self, key):
        p = self._path(key)
        return p.stat().st_size if p.exists() else None

    def delete(self, key):
        try:
            self._path(key).unlink()
        except OSError:
            pass


class S3Storage:
    """Any S3-compatible bucket. Endpoint and region come from the environment."""

    name = "s3"

    def __init__(self):
        import boto3
        from botocore.config import Config

        if not settings.S3_BUCKET:
            raise RuntimeError("CLIPPER_S3_BUCKET is not set")
        self.bucket = settings.S3_BUCKET
        self.prefix = settings.S3_PREFIX
        self.public_base = settings.S3_PUBLIC_BASE
        self.client = boto3.client(
            "s3",
            endpoint_url=settings.S3_ENDPOINT or None,
            region_name=settings.S3_REGION or None,
            aws_access_key_id=settings.S3_ACCESS_KEY or None,
            aws_secret_access_key=settings.S3_SECRET_KEY or None,
            config=Config(signature_version="s3v4",
                          retries={"max_attempts": 3, "mode": "standard"}),
        )

    @property
    def public(self):
        return bool(self.public_base)

    def _full(self, key):
        return f"{self.prefix}/{key}" if self.prefix else key

    def put(self, key, src, content_type=None, move=False):
        self.client.upload_file(
            str(src), self.bucket, self._full(key),
            ExtraArgs={"ContentType": content_type or guess_type(src)})
        if move:
            try:
                os.unlink(src)
            except OSError:
                pass
        return key

    def url(self, key):
        if self.public_base:
            return f"{self.public_base}/{self._full(key)}"
        return self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": self._full(key)},
            ExpiresIn=settings.SIGNED_URL_SECONDS)

    def local_path(self, key):
        return None             # nothing on this disk to point at

    def read(self, key):
        obj = self.client.get_object(Bucket=self.bucket, Key=self._full(key))
        return obj["Body"].read()

    def size(self, key):
        try:
            head = self.client.head_object(Bucket=self.bucket, Key=self._full(key))
            return head.get("ContentLength")
        except Exception:
            return None

    def delete(self, key):
        try:
            self.client.delete_object(Bucket=self.bucket, Key=self._full(key))
        except Exception:
            pass


_backend = None


def backend():
    global _backend
    if _backend is None:
        if settings.STORAGE_BACKEND == "s3":
            _backend = S3Storage()
        else:
            _backend = LocalStorage()
    return _backend


def store_clip(job_id, n, path, filename=None):
    """Put a rendered clip away. Returns (key, url, bytes)."""
    path = Path(path)
    key = clip_key(job_id, n, filename or path.name)
    store = backend()
    store.put(key, path, content_type="video/mp4")
    try:
        size = path.stat().st_size
    except OSError:
        size = store.size(key)
    return key, store.url(key), size
