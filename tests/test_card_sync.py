"""The integration serves custom_components/crew_hub/www/crew-hub-card.js; it must equal card/crew-hub-card.js.

Fix a failure with scripts/sync-card.sh.
"""

import os
import unittest

from _util import ROOT


class CardSyncTest(unittest.TestCase):
    def test_www_copy_is_identical(self):
        with open(os.path.join(ROOT, "card", "crew-hub-card.js"), "rb") as f:
            src = f.read()
        with open(os.path.join(ROOT, "custom_components", "crew_hub", "www", "crew-hub-card.js"), "rb") as f:
            served = f.read()
        self.assertEqual(src, served, "run scripts/sync-card.sh")


if __name__ == "__main__":
    unittest.main()
