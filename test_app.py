import base64
import tempfile
import unittest
from pathlib import Path

from app import BusinessError, ProvenanceStore


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ProvenanceStore(Path(self.tmp.name) / "test.db")
        self.store.seed()

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_provenance_and_return_review_flow(self):
        source = self.store.add_source("staff", "馆藏购藏档案", "archive", "ACC-1999-7")
        obj = self.store.create_object("staff", "M-1999-7", "青铜器", "礼器", "市博物馆", "1999年入藏，来源待持续核验。")
        event = self.store.add_event("staff", obj["id"], "acquisition", "1999-07-01", "", "本市", "从私人藏家购入", source["id"], "public")
        evidence = self.store.upload_evidence("staff", obj["id"], "purchase.pdf", base64.b64encode(b"purchase record").decode(), "internal", event["id"])
        self.assertEqual(len(evidence["sha256"]), 64)
        updated = self.store.update_object("staff", obj["id"], {"public_summary": "已完成首轮来源整理。"})
        self.assertEqual(updated["version"], 4)
        claim = self.store.create_claim("claimant1", obj["id"], "王氏家族", "返还藏品")
        self.store.transition_claim("reviewer1", claim["id"], "under_review", "材料齐全，进入调查。")
        self.store.transition_claim("reviewer1", claim["id"], "negotiating", "双方开始协商返还安排。")
        self.store.transition_claim("reviewer1", claim["id"], "resolved_return", "签署返还协议。")
        public_view = self.store.get_object("public", obj["id"])
        self.assertNotIn("current_holder", public_view)
        self.assertEqual(len(public_view["events"]), 1)
        self.assertEqual(public_view["claims"][0]["status"], "resolved_return")
        claimant_view = self.store.get_object("claimant1", obj["id"])
        self.assertEqual(len(claimant_view["claims"]), 1)
        self.assertGreaterEqual(len(self.store.object_history("reviewer1", obj["id"])), 6)

    def test_visibility_and_claim_stage_invariants(self):
        obj = self.store.create_object("staff", "M-2001-2", "手稿", "纸质", "资料室", "公开简介。")
        claim = self.store.create_claim("claimant1", obj["id"], "捐赠人后代", "归还手稿")
        with self.assertRaises(BusinessError) as ctx:
            self.store.transition_claim("reviewer1", claim["id"], "resolved_return", "直接结束。")
        self.assertEqual(ctx.exception.code, "invalid_transition")
        self.assertNotIn("claimant_id", self.store.get_object("public", obj["id"])["claims"][0])
        with self.assertRaises(BusinessError) as ctx:
            self.store.add_event("public", obj["id"], "note", "2020-01-01", "", "馆内", "未授权事件", None, "public")
        self.assertEqual(ctx.exception.status, 403)

    def _joint_fixture(self):
        obj = self.store.create_object("staff", "M-1943-1", "油画", "绘画", "市博物馆", "战时流转记录待核。")
        ev1 = self.store.upload_evidence("staff", obj["id"], "授权书-王.pdf", base64.b64encode(b"auth wang").decode(), "internal")
        ev2 = self.store.upload_evidence("staff", obj["id"], "授权书-李.pdf", base64.b64encode(b"auth li").decode(), "internal")
        claim1 = self.store.create_claim("claimant1", obj["id"], "王氏家族", "返还油画")
        claim2 = self.store.create_claim("claimant2", obj["id"], "李氏家族", "返还油画")
        case = self.store.create_joint_case("staff", obj["id"])
        p1 = self.store.add_party("staff", case["id"], claim1["id"], "王氏家族", "原藏家长孙", ev1["id"])
        p2 = self.store.add_party("staff", case["id"], claim2["id"], "李氏家族", "原藏家侄女", ev2["id"])
        return obj, ev1, ev2, claim1, claim2, case, p1, p2

    def test_joint_review_multi_claimant_flow(self):
        obj, ev1, ev2, claim1, claim2, case, p1, p2 = self._joint_fixture()
        self.assertEqual(case["status"], "pending_verification")
        panel = self.store.open_panel("reviewer1", case["id"], "第一次合议")
        self.store.cast_vote("reviewer1", panel["id"], "return", "来源链清晰应当返还。", [ev1["id"]], True)
        self.store.cast_vote("reviewer2", panel["id"], "return", "授权材料完备同意返还。", [ev2["id"]], False)
        # 资格未确认：案件停在待核验，返还结论不能生效
        with self.assertRaises(BusinessError) as ctx:
            self.store.conclude_panel("reviewer1", panel["id"], "return")
        self.assertEqual(ctx.exception.code, "qualification_unconfirmed")
        self.assertEqual(self.store.get_joint_case("reviewer1", case["id"])["status"], "pending_verification")
        self.store.set_qualification("reviewer1", case["id"], p1["id"], "confirmed", "身份关系与授权书一致。")
        result = self.store.set_qualification("reviewer2", case["id"], p2["id"], "confirmed", "经档案交叉核验通过。")
        self.assertEqual(result["case_status"], "in_review")
        result = self.store.conclude_panel("reviewer1", panel["id"], "return")
        self.assertTrue(result["effective"])
        self.assertEqual(result["case_status"], "concluded")
        # 结论生效后联动已确认资格参与人的主张
        claims = {c["id"]: c for c in self.store.get_object("staff", obj["id"])["claims"]}
        self.assertEqual(claims[claim1["id"]]["status"], "resolved_return")
        self.assertEqual(claims[claim2["id"]]["status"], "resolved_return")
        # 新来源材料补入：旧结论失效，并显示哪项依据变了
        self.store.upload_evidence("staff", obj["id"], "新出土交易记录.pdf", base64.b64encode(b"new record").decode(), "internal")
        view = self.store.get_joint_case("staff", case["id"])
        self.assertEqual(view["status"], "in_review")
        invalidated = view["panels"][0]
        self.assertFalse(invalidated["effective"])
        self.assertEqual(invalidated["invalidation"]["changed_basis"][0]["filename"], "新出土交易记录.pdf")
        self.assertEqual({b["id"] for b in invalidated["invalidation"]["panel_basis"]}, {ev1["id"], ev2["id"]})

    def test_joint_panel_quorum_and_agreement_rules(self):
        obj, ev1, ev2, claim1, claim2, case, p1, p2 = self._joint_fixture()
        self.store.set_qualification("reviewer1", case["id"], p1["id"], "confirmed", "身份关系与授权书一致。")
        self.store.set_qualification("reviewer2", case["id"], p2["id"], "confirmed", "经档案交叉核验通过。")
        panel = self.store.open_panel("reviewer1", case["id"], "第一次合议")
        self.store.cast_vote("reviewer1", panel["id"], "return", "应当返还原藏家。", [ev1["id"]], False)
        # 3 名审查员仅 1 人参与，未过半
        with self.assertRaises(BusinessError) as ctx:
            self.store.conclude_panel("reviewer1", panel["id"], "return")
        self.assertEqual(ctx.exception.code, "quorum_not_met")
        self.store.cast_vote("reviewer2", panel["id"], "reject", "证据不足反对返还。", [ev2["id"]], False)
        # 参与过半但同意返还者不足两人
        with self.assertRaises(BusinessError) as ctx:
            self.store.conclude_panel("reviewer1", panel["id"], "return")
        self.assertEqual(ctx.exception.code, "insufficient_agreement")
        # 同一审查员不能重复投票
        with self.assertRaises(BusinessError) as ctx:
            self.store.cast_vote("reviewer1", panel["id"], "reject", "改变主意了。", [ev1["id"]], False)
        self.assertEqual(ctx.exception.code, "vote_exists")

    def test_joint_case_public_and_claimant_visibility(self):
        obj, ev1, ev2, claim1, claim2, case, p1, p2 = self._joint_fixture()
        self.store.set_qualification("reviewer1", case["id"], p1["id"], "confirmed", "身份关系与授权书一致。")
        self.store.set_qualification("reviewer2", case["id"], p2["id"], "confirmed", "经档案交叉核验通过。")
        panel = self.store.open_panel("reviewer1", case["id"], "第一次合议")
        self.store.cast_vote("reviewer1", panel["id"], "return", "来源链清晰应当返还。", [ev1["id"]], True)
        self.store.cast_vote("reviewer2", panel["id"], "return", "授权材料完备同意返还。", [ev2["id"]], False)
        self.store.conclude_panel("reviewer1", panel["id"], "return")
        # 公众只看阶段和已公开意见
        pub = self.store.get_joint_case("public", case["id"])
        self.assertEqual(set(pub), {"id", "object_id", "status", "published_opinions"})
        self.assertEqual(pub["status"], "concluded")
        self.assertEqual(len(pub["published_opinions"]), 1)
        self.assertNotIn("evidence_ids", pub["published_opinions"][0])
        obj_pub = self.store.get_object("public", obj["id"])
        self.assertEqual(obj_pub["joint_case"]["status"], "concluded")
        self.assertNotIn("parties", obj_pub["joint_case"])
        # 主张人另可见自己的参与资格，看不到他人
        mine = self.store.get_joint_case("claimant1", case["id"])["my_parties"]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["qualification"], "confirmed")


if __name__ == "__main__":
    unittest.main()
