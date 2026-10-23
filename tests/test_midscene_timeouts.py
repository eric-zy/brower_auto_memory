import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kuaishou_upload.midscene_transport import MidsceneTransport
from kuaishou_upload.platforms.midscene_browser import MidscenePlatformBrowser


class MidsceneTimeoutTests(unittest.TestCase):
    def test_default_outer_timeout_covers_navigation_and_cleanup(self):
        with patch.dict(os.environ, {}, clear=True):
            transport = MidsceneTransport(Path(tempfile.gettempdir()))
            self.assertTrue(transport.wait_for_model)
            self.assertEqual(transport.timeout, 120)
            self.assertTrue(MidscenePlatformBrowser(None, Path(tempfile.gettempdir())).create_transport().wait_for_model)

    def test_custom_request_timeout_extends_navigation_and_rpc_together(self):
        with patch.dict(os.environ, {'MIDSCENE_WAIT_FOR_MODEL':'false','MIDSCENE_MODEL_TIMEOUT':'240000'}, clear=True):
            self.assertEqual(MidsceneTransport(Path(tempfile.gettempdir())).timeout,315)
        with patch.dict(os.environ, {'MIDSCENE_WAIT_FOR_MODEL':'false','MIDSCENE_MODEL_TIMEOUT':'120000','MIDSCENE_NAVIGATION_TIMEOUT':'240000'}, clear=True):
            self.assertEqual(MidsceneTransport(Path(tempfile.gettempdir())).timeout,285)

    def test_rejects_invalid_or_inverted_limits_before_starting_worker(self):
        for value in ('','abc','0','-1','999','600001','1e3','12.5'):
            with self.subTest(value=value), patch.dict(os.environ,{'MIDSCENE_WAIT_FOR_MODEL':'false','MIDSCENE_MODEL_TIMEOUT':value},clear=True):
                with self.assertRaises(ValueError):MidsceneTransport(Path(tempfile.gettempdir()))
        with patch.dict(os.environ,{'MIDSCENE_WAIT_FOR_MODEL':'false','MIDSCENE_MODEL_TIMEOUT':'90000','MIDSCENE_NAVIGATION_TIMEOUT':'90000'},clear=True):
            with self.assertRaisesRegex(ValueError,'必须大于'):MidsceneTransport(Path(tempfile.gettempdir()))

    def test_explicit_short_timeout_remains_available_for_local_protocol_tests(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(MidsceneTransport(Path(tempfile.gettempdir()),timeout=15).timeout,15)

    def test_default_wait_overrides_previous_fixed_limits(self):
        with patch.dict(os.environ,{'MIDSCENE_MODEL_TIMEOUT':'90000','MIDSCENE_NAVIGATION_TIMEOUT':'180000'},clear=True):
            self.assertTrue(MidsceneTransport(Path(tempfile.gettempdir())).wait_for_model)
