"""D-085: backups are fetched by the server through a presigned URL, so no backup byte passes through the CI runner."""
from __future__ import annotations

from datetime import datetime, timezone

from sealedstack import s3


def test_presigned_url_matches_the_published_aws_example():
    url = s3.presign_v4(
        "GET", "https://examplebucket.s3.amazonaws.com", "/test.txt",
        access_key="AKIAIOSFODNN7EXAMPLE", secret_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        region="us-east-1", now=datetime(2013, 5, 24, tzinfo=timezone.utc), expires=86400,
    )
    assert url.endswith("X-Amz-Signature=aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404")
    assert url.startswith("https://examplebucket.s3.amazonaws.com/test.txt?X-Amz-Algorithm=AWS4-HMAC-SHA256")


def test_the_client_presigns_one_object_path_style_for_r2():
    client = s3.S3Client("https://acct.r2.cloudflarestorage.com", "sealedstack-backups", "AK", "SK",
                         clock=lambda: datetime(2026, 11, 2, tzinfo=timezone.utc))
    url = client.presign_get("demo-app-db-x/demo-app/demoapp/2026.sql.gz", expires=900)
    assert url.startswith("https://acct.r2.cloudflarestorage.com/sealedstack-backups/demo-app-db-x/demo-app/demoapp/2026.sql.gz?")
    assert "X-Amz-Expires=900" in url and "SK" not in url.split("?", 1)[1].replace("X-Amz-SignedHeaders", "")
