import importlib.util, json, subprocess, tempfile, unittest
from pathlib import Path
spec=importlib.util.spec_from_file_location('deploy',Path(__file__).resolve().parents[1]/'tools/deploy.py')
deploy=importlib.util.module_from_spec(spec);spec.loader.exec_module(deploy)

class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.source=self.root/'source';self.target=self.root/'install'
        self.source.mkdir();self.target.mkdir()
        for args in [('init','-q'),('config','user.name','Test'),('config','user.email','test@example.invalid')]:
            subprocess.run(['git','-C',str(self.source),*args],check=True,capture_output=True)
        (self.source/'app.py').write_text('new\n');(self.source/'helper.py').write_text('helper\n')
        subprocess.run(['git','-C',str(self.source),'add','.'],check=True,capture_output=True)
        subprocess.run(['git','-C',str(self.source),'commit','-qm','fixture'],check=True,capture_output=True)
        (self.target/'app.py').write_text('old\n');(self.target/'characters.json').write_text('{"keep":true}')
        self.manifest=self.root/'manifest.json'
        self.manifest.write_text(json.dumps({'schema':1,'component':'test','files':[
            {'source':'app.py','destination':'app.py'}, {'source':'helper.py','destination':'helper.py'}]}))
    def test_apply_verify_rollback_preserves_unowned_state(self):
        plan=deploy.plan(self.source,self.target,self.manifest)
        self.assertEqual((self.target/'app.py').read_text(),'old\n')
        receipt=deploy.apply(plan);deploy.check(plan,installed=True)
        self.assertEqual((self.target/'characters.json').read_text(),'{"keep":true}')
        deploy.rollback(receipt)
        self.assertEqual((self.target/'app.py').read_text(),'old\n')
        self.assertFalse((self.target/'helper.py').exists())
    def test_destination_drift_refuses_all_writes(self):
        plan=deploy.plan(self.source,self.target,self.manifest)
        (self.target/'app.py').write_text('concurrent edit')
        with self.assertRaises(ValueError):deploy.apply(plan)
        self.assertEqual((self.target/'app.py').read_text(),'concurrent edit')
        self.assertFalse((self.target/'helper.py').exists())
    def test_source_drift_refuses_all_writes(self):
        plan=deploy.plan(self.source,self.target,self.manifest)
        (self.source/'helper.py').write_text('concurrent edit')
        with self.assertRaises(ValueError):deploy.apply(plan)
        self.assertEqual((self.target/'app.py').read_text(),'old\n')
    def test_path_traversal_rejected(self):
        with self.assertRaises(ValueError):deploy.within(self.target,'../outside')
    def test_rollback_refuses_later_edits(self):
        receipt=deploy.apply(deploy.plan(self.source,self.target,self.manifest))
        (self.target/'app.py').write_text('later edit')
        with self.assertRaises(ValueError):deploy.rollback(receipt)
        self.assertEqual((self.target/'app.py').read_text(),'later edit')

if __name__=='__main__':unittest.main()
