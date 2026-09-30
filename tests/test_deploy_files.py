"""Static checks of the deployment files: Ansible templates must render to valid Kubernetes YAML,
and the config they embed must be accepted by chaosevo itself. (Not a substitute for a real cluster.)"""
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HAVE = importlib.util.find_spec("jinja2") is not None and importlib.util.find_spec("yaml") is not None
ROLE = ROOT / "ansible" / "roles" / "chaosevo"


def _resolve(value, ctx, env):
    if isinstance(value, str):
        return env.from_string(value).render(**ctx) if "{{" in value else value
    if isinstance(value, dict):
        return {k: _resolve(v, ctx, env) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve(v, ctx, env) for v in value]
    return value


@unittest.skipUnless(HAVE, "needs jinja2 and PyYAML")
class DeployFilesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import jinja2
        import yaml
        cls.yaml = yaml
        cls.env = jinja2.Environment(undefined=jinja2.StrictUndefined)
        cls.env.filters["to_nice_json"] = lambda v: json.dumps(v, indent=2)
        cls.env.globals["lookup"] = cls._lookup
        cls.defaults = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())

    @staticmethod
    def _lookup(kind, arg):
        if kind == "pipe":
            return "20260101000000"
        if kind == "file":
            return Path(arg).read_text()
        raise ValueError(kind)

    def context(self, **overrides):
        ctx = {**self.defaults, **overrides, "playbook_dir": str(ROOT / "ansible"), "chaosevo_run_name": "20260101000000",
               "chaosevo_image": "registry.example/chaosevo:0.1.0"}
        for _ in range(4):                                    # defaults reference each other
            ctx = {k: _resolve(v, ctx, self.env) for k, v in ctx.items()}
        return ctx

    def render(self, name, **overrides):
        text = self.env.from_string((ROLE / "templates" / name).read_text()).render(**self.context(**overrides))
        return [d for d in self.yaml.safe_load_all(text) if d]

    def test_every_yaml_file_parses(self):
        for f in list((ROOT / "ansible").rglob("*.yml")) + list((ROOT / "prometheus").glob("*.yml")) + \
                 list((ROOT / "grafana").rglob("*.yml")) + list((ROOT / "observability").glob("*.yml")):
            with self.subTest(file=str(f.relative_to(ROOT))):
                self.assertIsNotNone(self.yaml.safe_load(f.read_text()))

    def test_dashboards_are_valid_json_with_unique_panel_ids(self):
        for f in (ROOT / "grafana" / "dashboards").glob("*.json"):
            dash = json.loads(f.read_text())
            ids = [p["id"] for p in dash["panels"]]
            self.assertEqual(len(ids), len(set(ids)), f.name)
            self.assertTrue(dash["uid"])

    def test_rbac_is_namespaced_and_least_privilege(self):
        docs = self.render("rbac.yml.j2")
        self.assertEqual([d["kind"] for d in docs], ["ServiceAccount", "Role", "RoleBinding"])   # no ClusterRole
        role = docs[1]
        self.assertEqual(role["metadata"]["namespace"], "demo")
        resources = {r for rule in role["rules"] for r in rule["resources"]}
        self.assertEqual(resources, {"pods", "deployments", "deployments/scale"})
        self.assertEqual(docs[2]["subjects"][0]["namespace"], "chaosevo")

    def test_rbac_grows_only_when_chaos_mesh_is_enabled(self):
        role = self.render("rbac.yml.j2", chaosevo_chaos_mesh=True)[1]
        self.assertIn("networkchaos", {r for rule in role["rules"] for r in rule["resources"]})

    def test_embedded_config_is_accepted_by_chaosevo(self):
        from chaosevo.cli import build_signals, load_config
        from chaosevo.fitness import FitnessSpec
        from chaosevo.ga import GAConfig
        cm = self.render("configmap.yml.j2", chaosevo_chaos_mesh=True)[0]
        cfg_json = json.loads(cm["data"]["config.json"])
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "c.json")
            Path(path).write_text(cm["data"]["config.json"])
            cfg = load_config(path, None)
        self.assertEqual(cfg["target"]["type"], "kubernetes")
        self.assertTrue(cfg_json["target"]["chaos_mesh"])
        GAConfig(**cfg["ga"])
        FitnessSpec.from_dicts(cfg["objectives"])
        signals = build_signals(cfg)
        self.assertEqual(signals[0].agg, "min")
        self.assertIn("kube_deployment_status_replicas_available", signals[0].query)
        self.assertTrue(cfg["prometheus"]["url"].startswith("http://kube-prometheus-stack-prometheus.monitoring"))

    def test_job_runs_non_root_read_only_and_exposes_metrics(self):
        job = self.render("job.yml.j2")[0]
        pod = job["spec"]["template"]["spec"]
        c = pod["containers"][0]
        self.assertEqual(job["metadata"]["name"], "chaosevo-20260101000000")
        self.assertTrue(pod["securityContext"]["runAsNonRoot"])
        self.assertTrue(c["securityContext"]["readOnlyRootFilesystem"])
        self.assertEqual(c["securityContext"]["capabilities"]["drop"], ["ALL"])
        self.assertIn("--metrics-port=9200", c["args"])
        self.assertEqual(job["spec"]["template"]["metadata"]["labels"], {"app": "chaosevo"})

    def test_service_servicemonitor_and_dashboard_line_up(self):
        svc = self.render("service.yml.j2")[0]
        mon = self.render("servicemonitor.yml.j2")[0]
        dash = self.render("dashboard-configmap.yml.j2")[0]
        self.assertEqual(mon["spec"]["selector"]["matchLabels"], svc["metadata"]["labels"])
        self.assertEqual(mon["spec"]["endpoints"][0]["port"], svc["spec"]["ports"][0]["name"])
        self.assertEqual(mon["metadata"]["labels"]["release"], "kube-prometheus-stack")
        self.assertEqual(dash["metadata"]["labels"], {"grafana_dashboard": "1"})
        self.assertEqual(json.loads(dash["data"]["chaosevo.json"])["uid"], "chaosevo-evolution")

    def test_demo_app_matches_the_default_target(self):
        deploy = self.render("demo-app.yml.j2")[0]
        self.assertEqual(deploy["metadata"]["name"], self.defaults["chaosevo_target_deployments"][0])

    def test_tasks_files_are_lists_of_named_tasks(self):
        for name in ("main.yml", "extras.yml"):
            tasks = self.yaml.safe_load((ROLE / "tasks" / name).read_text())
            self.assertTrue(all("name" in t for t in tasks), name)

    def test_placeholders_force_the_user_to_set_image_and_password(self):
        # the role must refuse to run with the placeholder image / no Grafana password
        self.assertIn("CHANGE-ME", self.defaults["chaosevo_image"])
        self.assertEqual(self.defaults["chaosevo_grafana_admin_password"], "")


if __name__ == "__main__":
    unittest.main()
