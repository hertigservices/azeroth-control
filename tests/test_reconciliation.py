import os, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "control"))
import control as C
import realms as R
import panel as P
import launcher as L

class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        R.bind(C); P.bind(C); L.bind(C)
    def test_process_scope_ignores_other_realm(self):
        with tempfile.TemporaryDirectory() as d:
            own=os.path.join(d,"own"); other=os.path.join(d,"other")
            rows=[{"pid":123,"ram_mb":8,"exe":os.path.join(other,"worldserver.exe")}]
            with patch.object(C,"procs",return_value=rows):
                self.assertEqual(C.running("worldserver",own),{"up":False,"others":1})
                self.assertEqual(C.running("worldserver",other)["pid"],123)
    def test_stop_does_not_signal_other_realm(self):
        with patch.object(C,"running",return_value={"up":False,"others":1}), patch.object(C,"ps") as ps:
            C.stop_graceful("worldserver",exe_dir="some-realm")
            ps.assert_not_called()
    def test_config_supplies_host_port_and_credentials(self):
        with tempfile.TemporaryDirectory() as d:
            conf=Path(d)/"custom.conf"
            conf.write_text('WorldDatabaseInfo = "127.0.0.2;3307;example;fixture-password;example_world"\n',encoding="utf8")
            db=C.dbinfo("world",str(conf))
            self.assertEqual((db["host"],db["port"],db["name"]),("127.0.0.2","3307","example_world"))
            self.assertEqual(R.db_password("example",str(conf)),"fixture-password")
            result=type("Result",(),{"returncode":0,"stdout":b"one\ttwo\n","stderr":b""})()
            with patch.object(C,"MYSQL","mysql"),patch.object(R.subprocess,"run",return_value=result) as run:
                rows,error=R.rows_as("example","SELECT 1",conf=str(conf))
            self.assertIsNone(error); self.assertEqual(rows,[["one","two"]])
            argv=run.call_args.args[0]
            self.assertIn("--port=3307",argv)
            self.assertNotIn("fixture-password"," ".join(argv))
    def test_panel_uses_world_override(self):
        r={"world":{"conf":"custom-world.conf"}}
        c={"realm":r,"db":{"user":"example"},"name":"Example","conf":"wrong"}
        with patch.object(R,"scalar_as",return_value=("1",None)) as scalar:
            P._scalar(c,"SELECT 1")
        self.assertEqual(scalar.call_args.kwargs["conf"],R.world_conf(r))

if __name__=="__main__": unittest.main()
