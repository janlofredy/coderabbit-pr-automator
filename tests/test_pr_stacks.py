"""
Unit tests for pr_stacks.py - Smart Ready to Merge & Stack Identification.
"""

import unittest
from pr_stacks import identify_pr_stacks
from daily_summary import compute_pr_summary_status


class TestPrStacks(unittest.TestCase):
    def test_single_pr_not_stacked(self):
        prs = [
            {
                "pr_key": "owner/repo#1",
                "repo": "owner/repo",
                "number": 1,
                "base_ref": "main",
                "head_ref": "feat-a",
                "ready_to_merge": True
            }
        ]
        result = identify_pr_stacks(prs)
        self.assertEqual(len(result), 1)
        pr = result[0]
        self.assertFalse(pr["is_stacked"])
        self.assertFalse(pr["is_stack_root"])
        self.assertFalse(pr["is_stack_blocked"])
        self.assertTrue(pr["ready_to_merge"])
        self.assertEqual(pr["stack_position"], 1)
        self.assertEqual(pr["stack_total"], 1)

    def test_linear_stack_hierarchy_and_blocking(self):
        prs = [
            {
                "pr_key": "owner/repo#1",
                "repo": "owner/repo",
                "number": 1,
                "title": "Part 1 (Root)",
                "base_ref": "main",
                "head_ref": "feat-1",
                "ready_to_merge": True
            },
            {
                "pr_key": "owner/repo#2",
                "repo": "owner/repo",
                "number": 2,
                "title": "Part 2 (Middle)",
                "base_ref": "feat-1",
                "head_ref": "feat-2",
                "ready_to_merge": True  # Approvals passed locally, but should be blocked by stack!
            },
            {
                "pr_key": "owner/repo#3",
                "repo": "owner/repo",
                "number": 3,
                "title": "Part 3 (Leaf)",
                "base_ref": "feat-2",
                "head_ref": "feat-3",
                "ready_to_merge": True  # Approvals passed locally, but should be blocked by stack!
            }
        ]

        result = identify_pr_stacks(prs)
        self.assertEqual(len(result), 3)

        p1 = next(p for p in result if p["number"] == 1)
        p2 = next(p for p in result if p["number"] == 2)
        p3 = next(p for p in result if p["number"] == 3)

        # PR 1 is the Root
        self.assertTrue(p1["is_stacked"])
        self.assertTrue(p1["is_stack_root"])
        self.assertFalse(p1["is_stack_blocked"])
        self.assertEqual(p1["stack_position"], 1)
        self.assertEqual(p1["stack_total"], 3)
        self.assertEqual(p1["stack_children_numbers"], [2])
        self.assertTrue(p1["ready_to_merge"])  # Root is ready to merge!

        # PR 2 is Middle
        self.assertTrue(p2["is_stacked"])
        self.assertFalse(p2["is_stack_root"])
        self.assertTrue(p2["is_stack_blocked"])  # Blocked by #1!
        self.assertEqual(p2["stack_parent_number"], 1)
        self.assertEqual(p2["stack_position"], 2)
        self.assertEqual(p2["stack_total"], 3)
        self.assertEqual(p2["stack_children_numbers"], [3])
        self.assertTrue(p2["local_ready_to_merge"])
        self.assertFalse(p2["ready_to_merge"])  # Smarter check: BLOCKED by stack!
        self.assertTrue(p2["ready_to_merge_blocked_by_stack"])
        self.assertIn("#1", p2["stack_block_reason"])

        # PR 3 is Leaf
        self.assertTrue(p3["is_stacked"])
        self.assertFalse(p3["is_stack_root"])
        self.assertTrue(p3["is_stack_blocked"])  # Blocked by #2!
        self.assertEqual(p3["stack_parent_number"], 2)
        self.assertEqual(p3["stack_position"], 3)
        self.assertEqual(p3["stack_total"], 3)
        self.assertEqual(p3["stack_children_numbers"], [])
        self.assertFalse(p3["ready_to_merge"])  # Smarter check: BLOCKED by stack!

    def test_unblocking_when_parent_pr_is_merged(self):
        # PR #1 was merged, so only PR #2 and PR #3 remain in open PRs
        prs = [
            {
                "pr_key": "owner/repo#2",
                "repo": "owner/repo",
                "number": 2,
                "title": "Part 2",
                "base_ref": "feat-1",
                "head_ref": "feat-2",
                "ready_to_merge": True
            },
            {
                "pr_key": "owner/repo#3",
                "repo": "owner/repo",
                "number": 3,
                "title": "Part 3",
                "base_ref": "feat-2",
                "head_ref": "feat-3",
                "ready_to_merge": True
            }
        ]

        result = identify_pr_stacks(prs)
        p2 = next(p for p in result if p["number"] == 2)
        p3 = next(p for p in result if p["number"] == 3)

        # PR #2 has no open parent in the repository, so it is now root and unblocked!
        self.assertTrue(p2["is_stack_root"])
        self.assertFalse(p2["is_stack_blocked"])
        self.assertTrue(p2["ready_to_merge"])

        # PR #3 is still blocked by PR #2
        self.assertTrue(p3["is_stack_blocked"])
        self.assertFalse(p3["ready_to_merge"])

    def test_branching_tree_stack(self):
        prs = [
            {
                "pr_key": "owner/repo#10",
                "repo": "owner/repo",
                "number": 10,
                "base_ref": "main",
                "head_ref": "feat-base",
                "ready_to_merge": True
            },
            {
                "pr_key": "owner/repo#11",
                "repo": "owner/repo",
                "number": 11,
                "base_ref": "feat-base",
                "head_ref": "feat-branch-a",
                "ready_to_merge": True
            },
            {
                "pr_key": "owner/repo#12",
                "repo": "owner/repo",
                "number": 12,
                "base_ref": "feat-base",
                "head_ref": "feat-branch-b",
                "ready_to_merge": True
            }
        ]

        result = identify_pr_stacks(prs)
        p10 = next(p for p in result if p["number"] == 10)
        p11 = next(p for p in result if p["number"] == 11)
        p12 = next(p for p in result if p["number"] == 12)

        self.assertEqual(sorted(p10["stack_children_numbers"]), [11, 12])
        self.assertTrue(p10["ready_to_merge"])
        self.assertEqual(p11["stack_parent_number"], 10)
        self.assertFalse(p11["ready_to_merge"])
        self.assertEqual(p12["stack_parent_number"], 10)
        self.assertFalse(p12["ready_to_merge"])

    def test_cross_repo_isolation(self):
        prs = [
            {
                "pr_key": "owner/repo1#1",
                "repo": "owner/repo1",
                "number": 1,
                "base_ref": "main",
                "head_ref": "shared-branch-name",
                "ready_to_merge": True
            },
            {
                "pr_key": "owner/repo2#2",
                "repo": "owner/repo2",
                "number": 2,
                "base_ref": "shared-branch-name",
                "head_ref": "feat-other",
                "ready_to_merge": True
            }
        ]

        result = identify_pr_stacks(prs)
        p1 = next(p for p in result if p["pr_key"] == "owner/repo1#1")
        p2 = next(p for p in result if p["pr_key"] == "owner/repo2#2")

        # In different repos, they should not stack together!
        self.assertFalse(p1["is_stacked"])
        self.assertFalse(p2["is_stacked"])
        self.assertTrue(p1["ready_to_merge"])
        self.assertTrue(p2["ready_to_merge"])

    def test_cycle_prevention(self):
        # A circular reference between two PR branches
        prs = [
            {
                "pr_key": "owner/repo#1",
                "repo": "owner/repo",
                "number": 1,
                "base_ref": "feat-b",
                "head_ref": "feat-a",
                "ready_to_merge": True
            },
            {
                "pr_key": "owner/repo#2",
                "repo": "owner/repo",
                "number": 2,
                "base_ref": "feat-a",
                "head_ref": "feat-b",
                "ready_to_merge": True
            }
        ]

        # Should safely terminate without infinite loop
        result = identify_pr_stacks(prs)
        self.assertEqual(len(result), 2)

    def test_text_based_dependency_detection(self):
        prs = [
            {
                "pr_key": "owner/repo#10",
                "repo": "owner/repo",
                "number": 10,
                "base_ref": "main",
                "head_ref": "feat-1",
                "ready_to_merge": True
            },
            {
                "pr_key": "owner/repo#11",
                "repo": "owner/repo",
                "number": 11,
                "title": "Feature B (Depends on #10)",
                "base_ref": "main",
                "head_ref": "feat-2",
                "ready_to_merge": True
            }
        ]

        result = identify_pr_stacks(prs)
        p11 = next(p for p in result if p["number"] == 11)
        self.assertTrue(p11["is_stacked"])
        self.assertTrue(p11["is_stack_blocked"])
        self.assertEqual(p11["stack_parent_number"], 10)
        self.assertFalse(p11["ready_to_merge"])

    def test_compute_pr_summary_status_with_stacks(self):
        # A blocked stacked PR should resolve to "Needs Work"
        blocked_pr = {
            "status_badge": "APPROVED_BY_YOU",
            "ready_to_merge": False,
            "is_stack_blocked": True,
            "stack_parent_number": 5
        }
        st = compute_pr_summary_status(blocked_pr)
        self.assertEqual(st, "Needs Work")

        # An unblocked root PR should resolve to "Ready to Merge"
        root_pr = {
            "status_badge": "APPROVED_BY_YOU",
            "ready_to_merge": True,
            "is_stack_blocked": False,
            "is_stack_root": True
        }
        st_root = compute_pr_summary_status(root_pr)
        self.assertEqual(st_root, "Ready to Merge")


if __name__ == "__main__":
    unittest.main()
