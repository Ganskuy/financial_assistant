"""Deterministic grounding regressions; these do not measure model accuracy."""

import pytest

from app.schemas.finance import TransactionDraft
from app.services.extraction import ground_transaction
from app.services.validation import parse_idr


def transaction(draft, **updates):
    return TransactionDraft.model_validate({**draft, **updates})


@pytest.mark.parametrize(
    ("text", "description", "merchant", "clean_description"),
    [
        ("Aku menambal ban di SPBU seharga 15.000", "Menambal ban di SPBU", "SPBU", "Menambal ban"),
        (
            "I repaired a tire at an SPBU for Rp15,000.",
            "Repaired tire at SPBU",
            "SPBU",
            "Repaired tire",
        ),
        ("Tadi beli nasi di warung 15rb", "Beli nasi di warung", "warung", "Beli nasi"),
        ("Bayar servis di bengkel Rp150.000", "Servis", "bengkel", "Servis"),
    ],
)
def test_restore_only_explicit_generic_business(
    draft, text, description, merchant, clean_description
):
    original = transaction(draft, merchant=None, description=description, amount=15000)
    grounded, reasons = ground_transaction(original, text)
    assert grounded.merchant == merchant
    assert grounded.description == clean_description
    assert "merchant_generic_restored" in reasons
    assert original.merchant is None
    assert grounded.model_dump(exclude={"merchant", "description"}) == original.model_dump(
        exclude={"merchant", "description"}
    )


@pytest.mark.parametrize(
    ("text", "merchant"),
    [
        ("Beli kopi di Kopi Kenangan 25rb", "Kopi Kenangan"),
        ("Paid Point Coffee Rp25.000 for coffee", "Point Coffee"),
        ("Gajian dari PT Maju 7 jt", "PT Maju"),
        ("Beli kopi di POINT COFFEE 25000", "Point Coffee"),
        ("Beli kopi di Café Élan 25000", "Café Élan"),
        ("Beli kopi di Warung Bu Sari dekat SPBU 15000", "Warung Bu Sari"),
        ("Beli bensin di SPBU Pertamina 50000", "SPBU Pertamina"),
        ("Beli kopi di Coffee & Co 25000", "Coffee & Co"),
        ("Kopi Kenangan 25rb untuk kopi", "Kopi Kenangan"),
        ("Shopee: beli kabel 15rb", "Shopee"),
        ("Beli kopi di Point Coffee 25000. Besok mau beli roti.", "Point Coffee"),
    ],
)
def test_preserve_explicit_named_business(draft, text, merchant):
    original = transaction(draft, merchant=merchant)
    grounded, reasons = ground_transaction(original, text)
    assert grounded == original
    assert reasons == []


@pytest.mark.parametrize(
    ("text", "merchant"),
    [
        ("Aku transfer 15.000 ke teman buat beli bensin", "SPBU"),
        ("Transfer 15000 buat beli bensin di SPBU", "SPBU"),
        ("Aku menambal ban di SPBU seharga 15.000", "Pertamina"),
        ("Ongkos ojek ke SPBU 15.000", "SPBU"),
        ("Beli kopi dekat SPBU 15.000", "SPBU"),
        ("Beli kopi di warung dekat SPBU 15.000", "SPBU"),
        ("Bayar taksi menuju Indomaret 15000", "Indomaret"),
        ("Paid for a taxi to SPBU for Rp15000", "SPBU"),
        ("Beli roti 15000, bukan di Indomaret", "Indomaret"),
        ("Beli roti di Indomaret atau Alfamart 15000, lupa", "Indomaret"),
        ("Beli Aqua 5000", "Aqua"),
        ("Beli pulsa 50000", "Telkomsel"),
        ("Beli kopi di rumah 15000", "rumah"),
        ("Beli kopi di Other Point Coffeehouse 25000", "Point Coffee"),
    ],
)
def test_clear_unsupported_product_destination_or_ambiguous_merchant(draft, text, merchant):
    grounded, reasons = ground_transaction(transaction(draft, merchant=merchant), text)
    # An explicitly grounded generic business may replace an unsupported model brand.
    expected = (
        "SPBU"
        if text == "Aku menambal ban di SPBU seharga 15.000"
        else ("warung" if text == "Beli kopi di warung dekat SPBU 15.000" else None)
    )
    assert grounded.merchant == expected
    assert any(reason in reasons for reason in ("merchant_unsupported", "merchant_role_ambiguous"))
    assert merchant not in reasons


@pytest.mark.parametrize(
    "text",
    [
        "Aku transfer 15.000 ke teman buat beli bensin",
        "Transfer 15000 buat beli bensin di SPBU",
        "Ongkos ojek ke SPBU 15.000",
        "Beli kopi dekat SPBU 15000",
        "Kalau beli bensin di SPBU 15000",
        "Mau beli bensin di SPBU 15000",
        "Beli bensin di SPBU atau warung 15000, lupa",
        "Beli kopi 25000",
        "Beli kopi di rumah 25000",
        "Beli kopi di toko Andi 25000",
        "Beli kopi di SPBU Pertamina 25000",
        "I plan to buy coffee at SPBU for Rp15000",
        "Tidak jadi beli bensin di SPBU 15000",
        "Bayar 15000 ke teman supaya menambal ban di SPBU",
        "Beli kopi di warung lalu beli bensin di SPBU 15000",
    ],
)
def test_missing_merchant_does_not_promote_context_or_named_fragment(draft, text):
    original = transaction(draft, merchant=None)
    grounded, reasons = ground_transaction(original, text)
    assert grounded == original
    assert reasons == []


def test_receipt_merchant_is_grounded_without_generic_promotion_or_description_rewrite(draft):
    original = transaction(draft, merchant="Point Coffee", description="Coffee at Point Coffee")
    text = "POINT COFFEE\n06/10/2026\nTOTAL 25.000"
    assert ground_transaction(original, text, receipt=True) == (original, [])
    missing, reasons = ground_transaction(
        transaction(draft, merchant=None), "SPBU\nTOTAL 25.000", receipt=True
    )
    assert missing.merchant is None
    assert reasons == []
    unsupported, reasons = ground_transaction(original, "SPBU\nTOTAL 25.000", receipt=True)
    assert unsupported.merchant is None
    assert reasons == ["merchant_unsupported"]


@pytest.mark.parametrize(
    "description",
    ["Travel to SPBU", "Coffee at Point Coffee with friends", "Point Coffee", "Coffee"],
)
def test_description_only_loses_literal_trailing_merchant_clause(draft, description):
    original = transaction(draft, description=description)
    grounded, _ = ground_transaction(original, "Beli kopi di Point Coffee 25000")
    assert grounded.description == description


@pytest.mark.parametrize(
    ("value", "amount"),
    [
        ("15.000", 15000),
        ("15rb", 15000),
        ("Rp15.000", 15000),
        ("1,5 juta", 1500000),
        ("7 jt", 7000000),
    ],
)
def test_existing_idr_normalization_supports_semantic_fixtures(value, amount):
    assert parse_idr(value) == amount
