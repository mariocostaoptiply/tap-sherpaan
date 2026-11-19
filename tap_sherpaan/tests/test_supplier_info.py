"""Offline supplier XML regressions; all values are synthetic."""

import copy
import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch
from xml.parsers.expat import ExpatError

from tenacity import RetryError, wait_none

from tap_sherpaan.client import SherpaStream
from tap_sherpaan.tap import TapSherpaan


def supplier_response(company="Synthetic Supplier", bank="TEST-ACCOUNT"):
    return f'''<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope">
<soap:Body><SupplierInfoResponse xmlns="http://sherpa.sherpaan.nl/">
<SupplierInfoResult><ResponseTime>72</ResponseTime><ResponseValue>
<SupplierCode>42</SupplierCode><BillingAddress>
<Name>Synthetic Contact</Name><Company>{company}</Company>
<TaxIdNumber>SYNTHETIC-TAX</TaxIdNumber><BankAccount>{bank}</BankAccount>
</BillingAddress><Token>7</Token></ResponseValue></SupplierInfoResult>
</SupplierInfoResponse></soap:Body></soap:Envelope>'''


class SupplierInfoTests(unittest.TestCase):
    def setUp(self):
        client_patch = patch("tap_sherpaan.client.SherpaClient")
        self.client = client_patch.start().return_value
        self.addCleanup(client_patch.stop)
        network_patch = patch(
            "requests.sessions.Session.request",
            side_effect=AssertionError("Unexpected network request"),
        )
        network_patch.start()
        self.addCleanup(network_patch.stop)
        retry_patch = patch.object(SherpaStream._make_soap_request.retry, "wait", wait_none())
        retry_patch.start()
        self.addCleanup(retry_patch.stop)
        self.tap = TapSherpaan(
            config={"shop_id": "synthetic", "security_code": "offline-placeholder"}
        )
        self.stream = self.tap.streams["supplier_info"]

    def extract(self, xml):
        self.client.call_custom_soap_service.return_value = {"raw_response": xml}
        return list(self.stream.get_records(context={"client_code": "42"}))

    def test_forbidden_controls_are_removed_from_extracted_supplier(self):
        for code in range(32):
            if code in (9, 10, 13):
                continue
            references = (
                chr(code),
                f"&#{code};",
                f"&#{code:04d};",
                f"&#x{code:x};",
                f"&#x{code:04X};",
            )
            for reference in references:
                with self.subTest(code=code, reference=repr(reference)):
                    with self.assertLogs(self.stream.logger, level="WARNING") as captured:
                        records = self.extract(supplier_response(bank=f"TEST{reference}ACCOUNT"))
                    self.assertEqual(len(records), 1)
                    self.assertEqual(records[0]["SupplierCode"], "42")
                    self.assertEqual(records[0]["Company"], "Synthetic Supplier")
                    self.assertEqual(records[0]["BankAccount"], "TESTACCOUNT")
                    warning = "\n".join(captured.output)
                    self.assertIn("supplier '42'", warning)
                    self.assertNotIn("TESTACCOUNT", warning)
                    self.assertNotIn("Synthetic Contact", warning)

    def test_valid_whitespace_unicode_and_references_are_preserved(self):
        records = self.extract(
            supplier_response(company="Caf\u00e9 &amp; Co&#9;&#10;&#13;&#x1F600; &amp;#x1C;")
        )
        self.assertEqual(records[0]["Company"], "Caf\u00e9 & Co\t\n\r\U0001f600 &#x1C;")

    def test_literal_references_survive_alongside_invalid_references(self):
        records = self.extract(
            supplier_response(
                company="<![CDATA[Literal &#x1C; & <text>]]><!-- &#28; --><?note &#x1C;?>",
                bank="TEST&#x1C;ACCOUNT",
            )
        )
        self.assertEqual(records[0]["Company"], "Literal &#x1C; & <text>")
        self.assertEqual(records[0]["BankAccount"], "TESTACCOUNT")

    def test_supplier_is_emitted_as_a_singer_record(self):
        self.client.call_custom_soap_service.return_value = {
            "raw_response": supplier_response(bank="TEST&#x1C;ACCOUNT")
        }
        output = io.StringIO()
        with redirect_stdout(output):
            self.stream.sync(context={"client_code": "42"})
        messages = [json.loads(line) for line in output.getvalue().splitlines()]
        records = [message["record"] for message in messages if message["type"] == "RECORD"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["SupplierCode"], "42")
        self.assertEqual(records[0]["BankAccount"], "TESTACCOUNT")

    def test_other_malformed_xml_fails_instead_of_skipping_supplier(self):
        for xml in (
            "<soap:Envelope>",
            supplier_response(company="Unescaped & text"),
            supplier_response(company="&#xD800;"),
            supplier_response(company="Broken & text", bank="TEST&#x1C;ACCOUNT"),
        ):
            with self.subTest(xml=xml):
                with self.assertRaises(RetryError) as raised:
                    self.extract(xml)
                self.assertIsInstance(raised.exception.last_attempt.exception(), ExpatError)

    def test_other_streams_are_not_cleaned_or_checkpointed_after_bad_xml(self):
        stream = self.tap.streams["changed_suppliers"]
        before = copy.deepcopy(self.tap.state)
        self.client.call_custom_soap_service.return_value = {
            "raw_response": supplier_response(company="Synthetic&#x1C;Supplier")
        }
        with self.assertRaises(RetryError) as raised:
            list(stream.get_records())
        self.assertIsInstance(raised.exception.last_attempt.exception(), ExpatError)
        self.assertEqual(self.tap.state, before)

    def test_empty_supplier_detail_stays_empty(self):
        xml = '''<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope">
<soap:Body><SupplierInfoResponse><SupplierInfoResult><ResponseValue/>
</SupplierInfoResult></SupplierInfoResponse></soap:Body></soap:Envelope>'''
        self.assertEqual(self.extract(xml), [])


if __name__ == "__main__":
    unittest.main()
