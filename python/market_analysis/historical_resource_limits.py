"""Stdlib-only bounded diagnostic contract shared by runtime and control transport."""
RESOURCE_REASONS = frozenset({'assembled-inventory-limit', 'transfer-limit',
                              'publication-disk-limit', 'staging-disk-limit'})
RESOURCE_MEASUREMENTS = frozenset({'measured_bytes', 'limit_bytes', 'free_bytes',
                                  'required_bytes', 'assembled_bytes', 'transfer_bytes'})


def resource_details(details):
    if (not isinstance(details, dict) or type(details.get('reason')) is not str
            or details['reason'] not in RESOURCE_REASONS):
        return {}
    return {'reason': details['reason'], **{key: value for key, value in details.items()
            if key in RESOURCE_MEASUREMENTS and type(value) is int and 0 <= value <= 2**63 - 1}}
