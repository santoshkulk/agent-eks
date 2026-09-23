import base64
import importlib.util
from pathlib import Path
import unittest


MODULE_DIR = Path(__file__).resolve().parents[1]
MODULE_PATH = MODULE_DIR / "scripts" / "validate_otlp_headers.py"
SPEC = importlib.util.spec_from_file_location("validate_otlp_headers", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Unable to load {MODULE_PATH}")
validate_otlp_headers = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validate_otlp_headers)


def encode_secret_value(raw_headers: str) -> str:
    """Encode a raw Kubernetes Secret value for a test input."""
    return base64.b64encode(raw_headers.encode("utf-8")).decode("ascii")


class OtlpHeaderValidationTests(unittest.TestCase):
    def test_accepts_nonempty_basic_credentials_and_ingestion_version(self) -> None:
        credentials = base64.b64encode(b"public-key:secret-key").decode("ascii")
        encoded = encode_secret_value(
            f"Authorization=Basic {credentials},x-langfuse-ingestion-version=4"
        )
        self.assertTrue(validate_otlp_headers.validate_encoded_headers(encoded))

    def test_rejects_invalid_outer_base64(self) -> None:
        self.assertFalse(validate_otlp_headers.validate_encoded_headers("!!!"))

    def test_rejects_invalid_basic_base64(self) -> None:
        encoded = encode_secret_value(
            "Authorization=Basic !!!,x-langfuse-ingestion-version=4"
        )
        self.assertFalse(validate_otlp_headers.validate_encoded_headers(encoded))

    def test_rejects_empty_basic_credentials(self) -> None:
        empty_credentials = base64.b64encode(b":").decode("ascii")
        encoded = encode_secret_value(
            f"Authorization=Basic {empty_credentials},"
            "x-langfuse-ingestion-version=4"
        )
        self.assertFalse(validate_otlp_headers.validate_encoded_headers(encoded))

    def test_rejects_missing_ingestion_version(self) -> None:
        credentials = base64.b64encode(b"public-key:secret-key").decode("ascii")
        encoded = encode_secret_value(f"Authorization=Basic {credentials}")
        self.assertFalse(validate_otlp_headers.validate_encoded_headers(encoded))


if __name__ == "__main__":
    unittest.main()
