from oslo_config import cfg

from gcl_sdk.audit import constants
from gcl_sdk.audit import opts


def test_get_audit_delivery_config_returns_registered_group():
    conf = cfg.ConfigOpts()
    opts.register_audit_delivery_opts(conf)

    delivery = opts.get_audit_delivery_config(conf)

    assert delivery is conf[constants.DOMAIN]
    assert delivery.enabled is False
    assert delivery.batch_size == 100
