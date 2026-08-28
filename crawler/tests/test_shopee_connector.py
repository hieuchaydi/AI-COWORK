"""
Tests for Shopee API connector and reviews sub-connector.
Updated to match the current ShopeeApiSource + ShopeeReviewsConnector API.
"""
import httpx
from crawler.connectors.shopee_api.source import ShopeeApiSource, TokenStore
from crawler.connectors.shopee_api.reviews import ShopeeReviewsConnector


def test_shopee_signature_and_reviews():
    client = httpx.AsyncClient()
    source = ShopeeApiSource(partner_id=12345, partner_key="secret_key", client=client)

    # ── Signature computation ──────────────────────────────────────────────
    sign = source.sign_request(
        path="/api/v2/product/get_item_list",
        timestamp=1700000000,
        access_token="tok123",
        shop_id=999,
    )
    assert len(sign) == 64  # SHA256 hex string
    # Deterministic
    sign2 = source.sign_request(
        path="/api/v2/product/get_item_list",
        timestamp=1700000000,
        access_token="tok123",
        shop_id=999,
    )
    assert sign == sign2

    # ── Reviews connector ──────────────────────────────────────────────────
    rev_connector = ShopeeReviewsConnector(source, buyer_hash_salt="test_salt")

    # PII: buyer ref is hashed
    hashed_buyer = rev_connector.hash_buyer("user_123")
    assert len(hashed_buyer) == 64
    assert hashed_buyer != "user_123"

    # Hashing is deterministic
    assert rev_connector.hash_buyer("user_123") == hashed_buyer

    # ── process_review: active review ────────────────────────────────────
    raw_active = {
        "comment_id": 101,
        "item_id": 202,
        "userid": "buyer_a",
        "rating_star": 5,
        "comment": "Hang rat tot",
        "create_time": 1700000000,
        "hidden": False,
        "reply": {},
    }
    rec1 = rev_connector.process_review(raw_active, shop_id=999)
    assert rec1["status"] == "visible"
    assert rec1["rating_star"] == 5
    assert rec1["buyer_ref"] == rev_connector.hash_buyer("buyer_a")
    assert len(rec1["content_hash"]) == 64
    assert rec1["dedup_key"] == "999:101"

    # ── process_review: hidden review ────────────────────────────────────
    raw_hidden = {
        "comment_id": 102,
        "item_id": 202,
        "userid": "buyer_b",
        "rating_star": 1,
        "comment": "Hang loi",
        "create_time": 1700000001,
        "hidden": True,
        "reply": {},
    }
    rec2 = rev_connector.process_review(raw_hidden, shop_id=999)
    assert rec2["status"] == "hidden"

    # ── mark_removed: soft-delete semantics ──────────────────────────────
    stored = dict(rec1)
    stored["last_seen_at"] = 1700000500
    removed = rev_connector.mark_removed(stored)
    assert removed["status"] == "removed"
    assert removed["last_seen_at"] == 1700000500  # NOT updated on removal
