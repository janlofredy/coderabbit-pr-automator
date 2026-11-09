"""
pr_stacks.py - Stack detection and dependency analysis for Pull Requests.

Identifies PR stacks (dependent pull requests where one PR branch is based on another)
and determines stack position, blocking relationships, and merge eligibility.
"""

import re
from typing import Dict, Any, List, Optional, Set

TRUNK_BRANCHES = {
    "main", "master", "trunk", "staging", "develop", "dev",
    "production", "prod", "release", "releases"
}

DEPENDENCY_REGEX = re.compile(
    r'(?:depends\s+on|stacked\s+on|parent\s+pr|blocked\s+by)\s*:?\s*#(\d+)',
    re.IGNORECASE
)


def _normalize_branch(branch: Optional[str]) -> str:
    if not branch:
        return ""
    # Strip remote prefix if present, e.g. "origin/feat" -> "feat"
    b = str(branch).strip()
    if b.startswith("origin/"):
        b = b[7:]
    return b.strip().lower()


def identify_pr_stacks(pull_requests: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Analyzes open pull requests, identifies PR stacks within each repository,
    and enriches each PR with stack hierarchy and smart ready-to-merge blocking.
    """
    if not pull_requests:
        return []

    # Group PRs by repository
    repos: Dict[str, List[Dict[str, Any]]] = {}
    for pr in pull_requests:
        repo = str(pr.get("repo") or "").strip().lower()
        repos.setdefault(repo, []).append(pr)

    for repo, pr_list in repos.items():
        # Build maps for the current repository
        head_map: Dict[str, Dict[str, Any]] = {}
        number_map: Dict[int, Dict[str, Any]] = {}

        for pr in pr_list:
            num = pr.get("number")
            if num is not None:
                try:
                    number_map[int(num)] = pr
                except (ValueError, TypeError):
                    pass

            head = _normalize_branch(pr.get("head_ref"))
            if head and head not in head_map:
                head_map[head] = pr

        # Detect direct parent for each PR
        # parent_of[child_num] = parent_num
        parent_of: Dict[int, int] = {}
        children_of: Dict[int, List[int]] = {}

        for pr in pr_list:
            child_num = pr.get("number")
            if child_num is None:
                continue
            child_num = int(child_num)

            base = _normalize_branch(pr.get("base_ref"))
            parent_pr: Optional[Dict[str, Any]] = None

            # 1. Branch-based stack detection: pr.base_ref matches another open PR's head_ref
            if base and base not in TRUNK_BRANCHES and base in head_map:
                candidate = head_map[base]
                cand_num = candidate.get("number")
                if cand_num is not None and int(cand_num) != child_num:
                    parent_pr = candidate

            # 2. Text-based stack dependency: title or body explicitly declares "Depends on #X"
            if not parent_pr:
                search_text = f"{pr.get('title', '')} {pr.get('body', '')}"
                match = DEPENDENCY_REGEX.search(search_text)
                if match:
                    try:
                        dep_num = int(match.group(1))
                        if dep_num in number_map and dep_num != child_num:
                            parent_pr = number_map[dep_num]
                    except (ValueError, TypeError):
                        pass

            if parent_pr:
                p_num = int(parent_pr.get("number", 0))
                # Cycle check: ensure child_num is not already an ancestor of p_num
                curr = p_num
                has_cycle = False
                visited = {child_num}
                while curr in parent_of:
                    if curr in visited:
                        has_cycle = True
                        break
                    visited.add(curr)
                    curr = parent_of[curr]

                if not has_cycle and p_num != child_num:
                    parent_of[child_num] = p_num
                    children_of.setdefault(p_num, []).append(child_num)

        # Annotate each PR in this repo
        for pr in pr_list:
            num = pr.get("number")
            if num is None:
                continue
            num = int(num)

            # Trace ancestors (parents up to root)
            ancestors: List[int] = []
            curr = num
            visited_ancestors: Set[int] = {curr}
            while curr in parent_of:
                parent_num = parent_of[curr]
                if parent_num in visited_ancestors:
                    break
                visited_ancestors.add(parent_num)
                ancestors.append(parent_num)
                curr = parent_num

            # Trace descendants (children down to leaves)
            descendants: List[int] = []
            queue = list(children_of.get(num, []))
            visited_descendants: Set[int] = set(queue) | {num}
            while queue:
                c_num = queue.pop(0)
                descendants.append(c_num)
                for next_c in children_of.get(c_num, []):
                    if next_c not in visited_descendants:
                        visited_descendants.add(next_c)
                        queue.append(next_c)

            is_stacked = bool(ancestors or descendants)
            is_stack_root = bool(not ancestors and descendants)
            is_stack_child = bool(ancestors)
            is_stack_blocked = bool(ancestors) # Parent PR is open in this repo

            direct_children = children_of.get(num, [])
            direct_parent_num = ancestors[0] if ancestors else None
            direct_parent_pr = number_map.get(direct_parent_num) if direct_parent_num else None

            stack_pos = len(ancestors) + 1
            stack_tot = len(ancestors) + 1 + len(descendants)

            if is_stacked:
                if is_stack_root:
                    child_str = ", #".join(str(c) for c in direct_children)
                    stack_label = f"Stack: 1 of {stack_tot} (Root • Blocks #{child_str})"
                else:
                    stack_label = f"Stack: {stack_pos} of {stack_tot} (Blocked by #{direct_parent_num})"
            else:
                stack_label = ""

            pr["is_stacked"] = is_stacked
            pr["is_stack_root"] = is_stack_root
            pr["is_stack_child"] = is_stack_child
            pr["is_stack_blocked"] = is_stack_blocked
            pr["stack_position"] = stack_pos if is_stacked else 1
            pr["stack_total"] = stack_tot if is_stacked else 1
            pr["stack_parent_number"] = direct_parent_num
            pr["stack_parent_title"] = direct_parent_pr.get("title", "") if direct_parent_pr else ""
            pr["stack_children_numbers"] = direct_children
            pr["stack_label"] = stack_label

            # Smart Ready to Merge Resolution
            local_ready = bool(pr.get("ready_to_merge", False) or pr.get("local_ready_to_merge", False))
            pr["local_ready_to_merge"] = local_ready

            if is_stack_blocked:
                # Blocked from merging because an open parent PR in the stack must merge first
                pr["ready_to_merge"] = False
                pr["ready_to_merge_blocked_by_stack"] = True
                if local_ready:
                    pr["stack_block_reason"] = f"Waiting for stack parent PR #{direct_parent_num} to merge first"
                else:
                    pr["stack_block_reason"] = f"Blocked by stack parent PR #{direct_parent_num}"
            else:
                pr["ready_to_merge"] = local_ready
                pr["ready_to_merge_blocked_by_stack"] = False
                pr["stack_block_reason"] = ""

    return pull_requests
