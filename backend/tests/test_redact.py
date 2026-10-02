from app.redact import Redactor


def test_masks_secret_values_and_params_in_messages():
    r = Redactor(["REALPASS", "33881"])
    data = {
        "supplier_response": {"success": False, "error": "ошибка сети: https://api.x/v2/add?login=u1&pass=REALPASS&art=W1"},
        "verification_message": "клиент 33881 не найден",
        "supplier_offer_id": "3388123",  # идентификатор: не трогаем, даже если похож на секрет
        "price": 33881,
        "reason": '{"user_password": "abc", "brand": "ZIC"}',
    }
    out = r.messages(data)
    assert "REALPASS" not in out["supplier_response"]["error"] and "u1" not in out["supplier_response"]["error"]
    assert "art=W1" in out["supplier_response"]["error"]
    assert out["verification_message"] == "клиент *** не найден"
    assert out["supplier_offer_id"] == "3388123" and out["price"] == 33881
    assert out["reason"] == '{"user_password": "***", "brand": "ZIC"}'


def test_url_encoded_secret():
    assert Redactor(["Pa$$/1"]).text("x?q=Pa%24%24%2F1") == "x?q=***"
