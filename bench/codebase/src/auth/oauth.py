"""OAuth device-code authorization for headless/CLI installs.

See TOP-114.
"""


def device_code_flow(client_id, poll_interval=5):
    """Start the device-code flow: returns a user_code and verification_url
    for the user to visit, then polls until the device is authorized."""
    device_code, user_code, verification_url = _request_device_code(client_id)
    token = _poll_for_token(device_code, poll_interval)
    return token


def _request_device_code(client_id):
    return "devcode", "ABCD-1234", "https://driftwood.example/activate"


def _poll_for_token(device_code, poll_interval):
    return {"access_token": "stub", "refresh_token": "stub"}
