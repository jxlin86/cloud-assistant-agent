"""Read OCI compartment names without involving the planner or changing resources."""
from pathlib import Path
import re
import threading
import time

_cache = {}
_lock = threading.Lock()


def lookup_compartment(settings, ocid=None):
    ocid = ocid or settings.compartment_id
    if not re.fullmatch(r'ocid1\.(?:compartment|tenancy)\.[a-z0-9.]+', ocid):
        raise ValueError('Enter a compartment or tenancy OCID.')
    key = (str(settings.config_file), settings.profile, settings.region, ocid)
    with _lock:
        cached = _cache.get(key)
        if cached and cached[0] > time.monotonic():
            return dict(cached[1])
    import oci
    config = oci.config.from_file(str(settings.config_file), settings.profile)
    options = dict(timeout=(3, 8), retry_strategy=oci.retry.NoneRetryStrategy())
    if settings.profile_uses_session():
        token = Path(config['security_token_file']).expanduser().read_text(encoding='utf-8').strip()
        private_key = oci.signer.load_private_key_from_file(str(Path(config['key_file']).expanduser()), config.get('pass_phrase'))
        options['signer'] = oci.auth.signers.SecurityTokenSigner(token, private_key)
    client = oci.identity.IdentityClient(config, **options)
    try:
        resource = (client.get_tenancy(ocid) if ocid.startswith('ocid1.tenancy.') else client.get_compartment(ocid)).data
        if resource.id != ocid or not resource.name:
            raise ValueError('Compartment identity could not be verified.')
        value = dict(id=resource.id, name=resource.name)
    finally:
        client.base_client.session.close()
    with _lock:
        if len(_cache) >= 128:
            _cache.pop(next(iter(_cache)))
        _cache[key] = (time.monotonic() + 300, value)
    return dict(value)
