# pipeline/notify.py
# Sends a Firebase Cloud Messaging push notification to all app subscribers
# after the pipeline completes. Uses the FCM HTTP v1 API with a service account.
#
# Required GitHub secret: FIREBASE_SERVICE_ACCOUNT_JSON
# (Project Settings → Service Accounts → Generate new private key)

import json
import os
import urllib.request
import urllib.error

from google.oauth2 import service_account
import google.auth.transport.requests

_FCM_SCOPES = ['https://www.googleapis.com/auth/firebase.messaging']
_TOPIC      = 'value_investor_data'
_CHANNEL_ID = 'value_investor_updates'


def send_data_update_notification(total_stocks: int, indices_count: int) -> None:
    sa_json = os.environ.get('FIREBASE_SERVICE_ACCOUNT_JSON')
    if not sa_json:
        print('FCM: FIREBASE_SERVICE_ACCOUNT_JSON not set — skipping notification')
        return

    try:
        sa_info    = json.loads(sa_json)
        project_id = sa_info['project_id']

        credentials = service_account.Credentials.from_service_account_info(
            sa_info, scopes=_FCM_SCOPES,
        )
        credentials.refresh(google.auth.transport.requests.Request())

        body = (
            f'Fresh weekly rankings are ready — '
            f'{total_stocks} stocks scored across {indices_count} indices.'
        )
        payload = json.dumps({
            'message': {
                'topic': _TOPIC,
                'notification': {
                    'title': 'New Data Available',
                    'body':  body,
                },
                'android': {
                    'notification': {'channel_id': _CHANNEL_ID},
                },
            }
        }).encode()

        url = f'https://fcm.googleapis.com/v1/projects/{project_id}/messages:send'
        req = urllib.request.Request(
            url, data=payload,
            headers={
                'Authorization': f'Bearer {credentials.token}',
                'Content-Type':  'application/json',
            },
            method='POST',
        )
        with urllib.request.urlopen(req) as resp:
            print(f'FCM: notification sent (HTTP {resp.status})')

    except Exception as e:
        print(f'FCM: failed to send notification — {e}')
