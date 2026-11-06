"""Trust signals & helpfulness (Prompt 16).

One-per-user revocable helpful votes on posts/answers, per-user helpfulness
rollups (lifetime + last-90-days), and community expert tiers (rising/known/
trusted) with ADMIN-TUNABLE threshold rows — no hardcoded persona content.
Other modules consume rolls through apps.trust.services public functions only.
"""
