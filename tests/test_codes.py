import json
import shutil
from decimal import Decimal
from pathlib import Path

import pytest

from einvoice_nrs import codes as nrs_codes
from einvoice_nrs.codes import CODES_VERSION, CodeListError, code_lists, load


def test_packaged_lists_load_and_match_the_manifest() -> None:
    codes = code_lists()
    assert codes.version == CODES_VERSION
    assert {"380", "381", "384"} <= codes.invoice_types
    assert "ZZZ" in codes.payment_means
    assert "NG" in codes.countries
    assert codes.currency_decimals["NGN"] == 2
    assert "2523.29" in codes.hs_codes
    assert "0111" in codes.service_codes
    assert "XBG" in codes.unit_codes


def test_tax_category_percents() -> None:
    percents = code_lists().tax_category_percents
    assert percents["STANDARD_VAT"] == Decimal("7.5")
    assert percents["ZERO_VAT"] == Decimal("0")
    assert percents["STATE_SALES_TAX"] is None  # "Not Available"
    assert percents["FUEL_EXCISE_TAX"] is None  # empty


def test_lga_in_state() -> None:
    codes = code_lists()
    assert codes.lga_in_state("NG-AB-ANO", "NG-AB")
    assert not codes.lga_in_state("NG-AB-ANO", "NG-LA")
    assert not codes.lga_in_state("NG-XX-NOPE", "NG-AB")


def _copy_lists(tmp_path: Path) -> Path:
    directory = tmp_path / "codes"
    shutil.copytree(Path(str(nrs_codes.PACKAGE_DIR)), directory)
    return directory


def test_edited_list_is_refused(tmp_path: Path) -> None:
    directory = _copy_lists(tmp_path)
    path = directory / "invoice_types.json"
    path.write_text(path.read_text().replace('"381"', '"999"'))
    with pytest.raises(CodeListError, match=r"invoice_types\.json doesn't match"):
        load(directory)


def test_wrong_count_is_refused(tmp_path: Path) -> None:
    directory = _copy_lists(tmp_path)
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["lists"]["states"]["count"] = 1
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(CodeListError, match=r"states\.json has 37 codes"):
        load(directory)
