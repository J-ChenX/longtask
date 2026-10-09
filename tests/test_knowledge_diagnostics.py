"""Actual knowledge defects are advisory, bound and independently consumable."""
import json
import os
import unittest
from unittest.mock import patch
import test_knowledge_context as fixtures

kc = fixtures.kc


class KnowledgeDiagnosticsTests(unittest.TestCase):
    setUp = fixtures.KnowledgeContextTests.setUp
    write = fixtures.KnowledgeContextTests.write
    workspace = fixtures.KnowledgeContextTests.workspace
    cli = fixtures.KnowledgeContextTests.cli
    assert_error = fixtures.KnowledgeContextTests.assert_error

    def test_actual_findings_orphan_link_purpose_contract_and_process_are_located(self):
        self.write('docs/ARCHITECTURE.md', '# 项目\n\n项目用途。\n[支付](modules/支付结算.md#接口)\n[缺失](modules/不存在.md)\n<!-- contract: IF-PAY-01 -->\n')
        self.write('docs/modules/支付结算.md', '# 支付结算\n\n处理结算。\n\n## 接口\n合同 ID: IF-PAY-01\n[失效](#旧接口)\n本次会话的任务进度：调查中断。\n')
        self.write('docs/modules/孤立.md', '# 孤立模块\n')
        before = {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        result = self.cli('doctor')
        after = {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        by_code = {item['code']: item for item in result['findings']}
        expected = {'orphan_document', 'missing_purpose_clue', 'broken_document_reference',
                    'broken_section_reference', 'duplicate_contract_id', 'possible_task_process'}
        self.assertEqual(set(by_code), expected)
        self.assertEqual(by_code['orphan_document']['path'], 'docs/modules/孤立.md')
        self.assertEqual(by_code['duplicate_contract_id']['contract_id'], 'IF-PAY-01')
        self.assertEqual(len(by_code['duplicate_contract_id']['locations']), 2)
        self.assertTrue(result['read_only'])
        self.assertTrue(result['advisory_only'])
        self.assertEqual(result['semantic_freshness'], 'not_determined')
        self.assertFalse(result['trust']['grants_authorization'])
        for document in result['documents']:
            self.assertEqual(document['file_sha256'], kc.sha256((self.root / document['path']).read_bytes()))

    def test_explicit_contract_tables_detect_duplicates_but_references_do_not_define_contracts(self):
        self.write('docs/ARCHITECTURE.md', '# 项目\n\n用途。\n[模块](modules/支付结算.md)\nContract-ID: IF-ONE\n')
        self.write('docs/modules/支付结算.md', '# 模块\n\n接口用途。\n| 接口 ID | 签名 |\n|---|---|\n| IF-ONE | read() |\n\n仅引用 IF-ONE 不定义合同。\n')
        result = self.cli('diagnose')
        duplicate = next(f for f in result['findings'] if f['code'] == 'duplicate_contract_id')
        self.assertEqual(duplicate['contract_id'], 'IF-ONE')
        self.assertEqual(len(duplicate['locations']), 2)

    def test_navigation_metadata_and_declarations_alone_are_not_purpose_clues(self):
        self.write('docs/ARCHITECTURE.md', '# 项目\n\n来源：observed\nContract-ID: IF-ONE\n[模块](modules/支付结算.md)\n')
        result = self.cli('diagnose')
        self.assertTrue(any(f['code'] == 'missing_purpose_clue' and f['path'] == 'docs/ARCHITECTURE.md' for f in result['findings']))

    def test_reference_definitions_and_expanded_l2_are_reachable_from_l0(self):
        self.write('docs/ARCHITECTURE.md', '# 项目\n\n有用途。\n[模块][pay]\n[pay]: modules/支付结算.md\n')
        self.write('docs/modules/支付结算.md', '# 模块\n\n处理结算。\n[专项](支付结算/恢复.md#恢复)\n')
        self.write('docs/modules/支付结算/恢复.md', '# 专项\n\n运维用途。\n\n## 恢复\n重试输入观察。\n')
        result = self.cli('diagnose')
        self.assertEqual(result['findings'], [])
        self.assertTrue(result['complete'])

    def test_style_changes_dates_hypotheses_and_fenced_examples_do_not_mean_semantic_staleness(self):
        self.write('docs/ARCHITECTURE.md', '# A different title\n\nLong term purpose.\n[模块](modules/支付结算.md)\n')
        self.write('docs/modules/支付结算.md', '# 随意标题\n\n模块用途。\n来源：inferred\n最后更新：2001-01-01\n当前限制需在网络恢复时复核。\n```md\n[不存在](不存在.md)\n<!-- contract: IF-X -->\n本次会话\n```\n')
        result = self.cli('diagnose')
        self.assertEqual(result['findings'], [])
        self.assertEqual(result['semantic_freshness'], 'not_determined')

    def test_unselected_markdown_target_is_not_followed_or_read_and_anchor_not_claimed_valid(self):
        self.write('docs/ARCHITECTURE.md', '# 项目\n\n用途。\n[外部](../手册.md#不存在)\n')
        self.write('手册.md', '# 外部\n\n' + 'x' * (kc.MAX_FILE_BYTES + 1))
        result = self.cli('diagnose', '--path', 'docs/ARCHITECTURE.md')
        self.assertEqual([f['code'] for f in result['findings']], ['reference_outside_scan'])
        self.assertEqual(len(result['documents']), 1)

    def test_output_budget_limit_pagination_and_no_empty_success(self):
        self.write('docs/ARCHITECTURE.md', '# 项目\n\n用途。\n' + ''.join(f'[失效{i}](modules/缺失{i}.md)\n' for i in range(20)))
        result = self.cli('diagnose', '--limit', '3', '--budget-bytes', '1800')
        self.assertFalse(result['complete'])
        self.assertLessEqual(len(kc.encoded_json(result)) + 1, 1800)
        self.assertEqual(result['budget']['output_bytes'], len(kc.encoded_json(result)) + 1)
        self.assertEqual(result['next_offset'], 3)
        second = self.cli('diagnose', '--offset', '3', '--limit', '3')
        self.assertNotEqual(result['findings'], second['findings'])
        error = self.cli('diagnose', '--budget-bytes', '100', okay=False)
        self.assertEqual(error['error']['code'], 'output_budget_too_small')

    def test_symlink_tree_and_target_are_refused(self):
        self.write('docs/ARCHITECTURE.md', '# 项目\n\n用途。\n[手册](../链接.md)\n')
        target = self.write('手册.md', '# 手册\n\n用途。\n')
        os.symlink(target, self.root / '链接.md')
        error = self.cli('diagnose', '--path', 'docs/ARCHITECTURE.md', okay=False)
        self.assertEqual(error['error']['code'], 'unsafe_path')

    def test_changed_read_source_and_unselected_target_are_rejected_before_output(self):
        self.write('docs/ARCHITECTURE.md', '# 项目\n\n用途。\n[手册](../手册.md)\n')
        target = self.write('手册.md', '# 手册\n\n用途。\n')
        workspace = self.workspace()
        original_verify = workspace.verify
        def change_then_verify():
            target.write_text('# 修改过的手册\n')
            original_verify()
        with patch.object(workspace, 'verify', side_effect=change_then_verify):
            self.assert_error('read_race', lambda: kc.diagnose(workspace))

    def test_ambiguous_anchor_reference_and_escaping_link_never_pick_first_target(self):
        self.write('docs/ARCHITECTURE.md', '# 项目\n\n用途。\n[重复](modules/支付结算.md#接口)\n[越界](../../outside.md)\n[未知][missing]\n')
        self.write('docs/modules/支付结算.md', '# 模块\n\n用途。\n## 接口\n正文。\n## 接口\n第二段。\n')
        result = self.cli('diagnose')
        codes = {f['code'] for f in result['findings']}
        self.assertTrue({'ambiguous_section', 'broken_section_reference', 'invalid_local_reference', 'broken_reference_definition'} <= codes)


if __name__ == '__main__':
    unittest.main()
