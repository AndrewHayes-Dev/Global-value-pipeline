# pipeline/uploader.py
# Uploads processed JSON/xlsx files to Cloudflare R2 (global-investor-data bucket).

import json
import math
import os
from typing import Any

import boto3
from botocore.config import Config


def _sanitize(obj: Any) -> Any:
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    return obj


def _r2_client():
    account_id = os.environ['CLOUDFLARE_ACCOUNT_ID']
    return boto3.client(
        's3',
        endpoint_url=f'https://{account_id}.r2.cloudflarestorage.com',
        aws_access_key_id=os.environ['GLOBAL_R2_ACCESS_KEY_ID'],
        aws_secret_access_key=os.environ['GLOBAL_R2_SECRET_ACCESS_KEY'],
        config=Config(signature_version='s3v4'),
        region_name='auto',
    )


def upload_json(key: str, data: Any, bucket: str) -> bool:
    body = json.dumps(_sanitize(data), separators=(',', ':'), default=str).encode('utf-8')
    try:
        client = _r2_client()
        client.put_object(
            Bucket=bucket,
            Key=key,
            Body=body,
            ContentType='application/json',
        )
        print(f'  Uploaded: {key} ({len(body):,} bytes)')
        return True
    except Exception as e:
        print(f'  Upload failed [{key}]: {e}')
        return False


def upload_bytes(key: str, data: bytes, content_type: str, bucket: str) -> bool:
    try:
        client = _r2_client()
        client.put_object(
            Bucket=bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
        )
        print(f'  Uploaded: {key} ({len(data):,} bytes)')
        return True
    except Exception as e:
        print(f'  Upload failed [{key}]: {e}')
        return False
