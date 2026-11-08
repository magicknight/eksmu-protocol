"""Explicitly disabled external anchoring boundary for the private prototype.

No network client, credentials, queue, or enable switch belongs to this module.
"""
from .store import Problem


class DisabledAnchor:
    def status(self):
        return {'state': 'disabled', 'provider': None, 'client_version': None,
                'proof': None, 'submission_available': False}

    def submit(self, _commitment_sha256):
        raise Problem('外部时间存证未启用；本实例不会提交承诺', 503)
