"""Export-policy gate for Python plugins used by standalone PWA snapshots."""

from .errors import SnapshotBuildError


# Each allowed plugin is still constrained by reconciliation: unchanged entries
# are emitted from source text, generated entries are printed, and transformed
# source entries may only use the builder's guarded transformed-output policy.
SUPPORTED_PLUGINS = frozenset(
    {
        "beancount.ops.documents",
        "beancount.ops.pad",
        "beancount.ops.balance",
        "beancount.plugins.close_tree",
        "beancount.plugins.coherent_cost",
        "beancount.plugins.currency_accounts",
        "beancount.plugins.implicit_prices",
        "beancount_lazy_plugins.valuation",
        "beancount_lazy_plugins.generate_inverse_prices",
        "beancount_lazy_plugins.generate_base_ccy_prices",
        "beancount_lazy_plugins.group_pad_transactions",
        "beancount_lazy_plugins.filter_map",
        "beancount_lazy_plugins.auto_accounts",
        "beancount_share.share",
        "beancount_reds_plugins.effective_date.effective_date",
        "beancount_interpolate.recur",
        "beancount_interpolate.split",
    }
)


def validate_source_preserving_plugins(plugin_names: set[str]) -> None:
    """Reject plugin execution for which no export safety gate is registered."""
    unsupported = sorted(plugin_names - SUPPORTED_PLUGINS)
    if unsupported:
        raise SnapshotBuildError(
            "Standalone snapshot has no export policy for plugin(s): "
            + ", ".join(unsupported)
        )
