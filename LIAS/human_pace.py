"""Human pacing (anti-bot) — guide step 9 / קצב אנושי (אנטי-בוט) — שלב 9.

EN: ALL waiting between portal actions goes through this one class, so the
    behavior profile is tuned in a single place. Randomized jitter + an
    occasional longer "reading pause". The User-Agent stays stable per
    session on purpose — rotating UA mid-session on a logged-in government
    site is a red flag, not camouflage.
HE: כל ההמתנות בין פעולות מול הפורטל עוברות דרך המחלקה האחת הזו, כך
    שפרופיל ההתנהגות מכוון במקום אחד. Jitter אקראי + הפסקת "קריאה"
    ארוכה מדי פעם. ה-User-Agent נשאר יציב לאורך הסשן בכוונה — החלפת
    UA באמצע סשן ממשלתי מחובר היא דגל אדום, לא הסוואה.
"""
from __future__ import annotations

import random
import time

from . import config


class HumanPace:
    def __init__(self) -> None:
        self._actions = 0

    def wait(self) -> float:
        """Call before every click/navigation / לקרוא לפני כל לחיצה/ניווט."""
        self._actions += 1
        delay = random.uniform(config.JITTER_MIN_SEC, config.JITTER_MAX_SEC)
        # occasional long "reading" pause / הפסקת "קריאה" ארוכה מדי פעם
        if self._actions % config.LONG_PAUSE_EVERY_N_ACTIONS == 0:
            delay += random.uniform(*config.LONG_PAUSE_RANGE_SEC)
        time.sleep(delay)
        return delay
