import asyncio

import pytest

from autohunt.tab_cleanup import close_duplicate_hh_pages


def test_cleanup_rejects_zero_keep():
    with pytest.raises(ValueError):
        asyncio.run(close_duplicate_hh_pages([], keep=0))
