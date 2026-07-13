"""NET HaMishpat navigation — tab transitions and party/case extraction."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from playwright.sync_api import Page

if TYPE_CHECKING:
    from core.logger import Logger


class NoTikNiyarTab(Exception):
    """Raised when the 'תיק נייר' tab is not found on the current case page."""


class NetNavigator:
    def __init__(self, page: Page, logger: "Logger | None" = None) -> None:
        self.page = page
        self.logger = logger

    def _log(self, msg: str, level: str = "info") -> None:
        prefixed = f"[NET Navigator] {msg}"
        if self.logger:
            getattr(self.logger, level)(prefixed)
        else:
            print(prefixed)

    def _ui_navigate(self, label: str) -> bool:
        self._log(f"Navigating to '{label}'...")
        locator = self.page.locator(
            f"a:has-text('{label}'), span:has-text('{label}')"
        ).first

        if locator.count() == 0:
            self._log(f"WARNING: Element '{label}' not found on page!", "warn")
            return False

        try:
            js_code = locator.get_attribute("href") or ""
            if "javascript:" in js_code:
                self.page.evaluate(js_code.replace("javascript:", ""))
            else:
                locator.click(force=True)
            time.sleep(5)
            self._log(f"Arrived at '{label}'.")
            return True
        except Exception as e:
            self._log(f"Navigation error for '{label}': {e}", "error")
            return False

    def extract_parties(self) -> list[str]:
        """Navigate to 'צדדים' tab and return all party names listed in the grid."""
        self._ui_navigate("צדדים")
        try:
            parties: list[str] = self.page.evaluate(
                """() => {
                    const rows = Array.from(
                        document.querySelectorAll('div[role="row"][row-id]')
                    );
                    const seen = new Set();
                    const result = [];
                    for (const row of rows) {
                        const nameCell = row.querySelector('div[col-id="FullName"]');
                        if (nameCell) {
                            const name = nameCell.innerText.trim();
                            if (name && !seen.has(name)) {
                                seen.add(name);
                                result.push(name);
                            }
                        }
                    }
                    return result;
                }"""
            )
            self._log(f"Parties found: {parties}")
            return parties or []
        except Exception as e:
            self._log(f"Party extraction error: {e}", "error")
            return []

    def extract_parties_full(self) -> list[dict]:
        """Navigate to צדדים tab. Return list of dicts with keys:
        'name', 'role', 'representative'
        Uses the confirmed col-ids from NET portal HTML."""
        self._ui_navigate("צדדים")
        # Wait briefly for the ag-grid to render
        import time as _time
        _time.sleep(1)
        try:
            data = self.page.evaluate("""() => {
                const rows = Array.from(document.querySelectorAll('.ag-row[role="row"]'));
                return rows.map(row => {
                    const get = colId => {
                        const cell = row.querySelector('[col-id="' + colId + '"]');
                        return cell ? cell.innerText.trim() : '';
                    };
                    return {
                        name: get('FullName'),
                        role: get('RoleName'),
                        representative: get('RepresentatedOrRepresentativesNames'),
                    };
                }).filter(r => r.name || r.role);
            }""")
            self._log(f"Full party data: {data}")
            return data or []
        except Exception as e:
            self._log(f"extract_parties_full error: {e}", "error")
            return []

    def extract_parties_table(self) -> list[dict]:
        """Read the ag-grid parties table and return structured rows.

        Each row has keys: 'role', 'name', 'representative'.
        Uses RoleName / FullName / RepresentatedOrRepresentativesNames col-ids.
        """
        try:
            data = self.page.evaluate("""() => {
                const rows = document.querySelectorAll('.ag-row[role="row"]');
                return Array.from(rows).map(row => {
                    const get = colId => {
                        const cell = row.querySelector('[col-id="' + colId + '"]');
                        return cell ? cell.innerText.trim() : '';
                    };
                    return {
                        role: get('RoleName'),
                        name: get('FullName'),
                        representative: get('RepresentatedOrRepresentativesNames'),
                    };
                }).filter(r => r.name || r.role);
            }""")
            self._log(f"Parties table: {data}")
            return data or []
        except Exception as e:
            self._log(f"extract_parties_table error: {e}", "error")
            return []

    def identify_our_side(self, parties: list[dict], our_lawyer_name: str) -> str:
        """Return 'תובע' or 'נתבע' based on which party the lawyer represents.

        Performs a case-insensitive partial match against the 'representative' field.
        Returns '' if not found.
        """
        if not our_lawyer_name:
            return ""
        name_lower = our_lawyer_name.lower()
        for party in parties:
            rep = party.get("representative", "")
            if rep and name_lower in rep.lower():
                role = party.get("role", "")
                if "תובע" in role:
                    return "תובע"
                if "נתבע" in role:
                    return "נתבע"
        return ""

    def extract_our_side(self, our_lawyer_name: str) -> str:
        """Navigate to parties tab, extract table, and return 'תובע'/'נתבע'/''."""
        self._ui_navigate("צדדים")
        parties = self.extract_parties_table()
        return self.identify_our_side(parties, our_lawyer_name)

    def get_representatives(self) -> list[str]:
        """Return unique non-empty representative names from party grid."""
        parties = self.extract_parties_full()
        seen = set()
        result = []
        for p in parties:
            rep = p.get("representative", "").strip()
            if rep and rep not in seen:
                seen.add(rep)
                result.append(rep)
        return result

    def extract_case_metadata(self) -> dict:
        """Extract case-level metadata from the current NET portal page.

        Returns a dict with keys:
          case_title, procedure, case_number, court, judge, open_date, close_date
        All values are strings; missing values are empty strings.
        """
        meta: dict = {
            "case_title": "",
            "procedure": "",
            "case_number": "",
            "court": "",
            "judge": "",
            "open_date": "",
            "close_date": "",
            "our_side": "",
        }
        try:
            # Case title is typically in a header span / toolbar
            title_js = """() => {
                const selectors = [
                    'span[id*="CaseTitle"]', 'span[id*="caseTitle"]',
                    'span[id*="lblCaseName"]', 'span[id*="CaseName"]',
                    'div[id*="CaseTitle"]', 'h1', 'h2',
                    '.case-title', '#caseHeader',
                ];
                for (const s of selectors) {
                    const el = document.querySelector(s);
                    if (el && el.innerText && el.innerText.trim().length > 3)
                        return el.innerText.trim();
                }
                // Fallback: look for a span containing a case-number pattern
                const all = Array.from(document.querySelectorAll('span, td, div'));
                for (const el of all) {
                    if (/\\d{3,6}-\\d{2}-\\d{2}/.test(el.innerText) && el.innerText.length < 200)
                        return el.innerText.trim();
                }
                return "";
            }"""
            meta["case_title"] = self.page.evaluate(title_js) or ""

            # Court name
            court_js = """() => {
                const selectors = [
                    'span[id*="Court"]', 'span[id*="court"]',
                    'span[id*="lblCourt"]', 'td[id*="Court"]',
                    'span[id*="CourtName"]',
                ];
                for (const s of selectors) {
                    const el = document.querySelector(s);
                    if (el && el.innerText && el.innerText.trim()) return el.innerText.trim();
                }
                return "";
            }"""
            meta["court"] = self.page.evaluate(court_js) or ""

            # Judge
            judge_js = """() => {
                const selectors = [
                    'span[id*="Judge"]', 'span[id*="judge"]',
                    'span[id*="lblJudge"]', 'td[id*="Judge"]',
                ];
                for (const s of selectors) {
                    const el = document.querySelector(s);
                    if (el && el.innerText && el.innerText.trim()) return el.innerText.trim();
                }
                return "";
            }"""
            meta["judge"] = self.page.evaluate(judge_js) or ""

            # Parse procedure and case_number from title
            import re as _re
            t = meta["case_title"]
            m = _re.match(r"^([^\d]+?)\s+(\d{3,6}-\d{2}-\d{2})", t)
            if m:
                meta["procedure"] = m.group(1).strip(' "\'')
                meta["case_number"] = m.group(2)
            else:
                m2 = _re.search(r"(\d{3,6}-\d{2}-\d{2})", t)
                if m2:
                    meta["case_number"] = m2.group(1)

        except Exception as e:
            self._log(f"extract_case_metadata error: {e}", "warn")

        self._log(f"Case metadata: {meta}")
        return meta

    def navigate_to_tik_niyar(self) -> None:
        """Navigate to the 'תיק נייר' documents tab.

        Raises NoTikNiyarTab if the tab is not present on this case.
        """
        found = self._ui_navigate("תיק נייר")
        if not found:
            raise NoTikNiyarTab(
                "תיק נייר tab not found — the case may not have a document grid yet."
            )
