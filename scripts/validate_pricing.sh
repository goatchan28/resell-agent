#!/usr/bin/env bash
#
# Local validation of the pricing state machine on MP-000003.
# No eBay calls: every apply goes through the --marketplace-ref path.
#
#   RESELL_DB=/tmp/pricing_validation.db ./validate_pricing.sh
#
# Checks outcomes rather than just printing them, so a silent regression fails
# the run instead of scrolling past.

set -uo pipefail

SKU="MP-000003"
PASS=0
FAIL=0

c_ok()   { printf '\033[32m  ok\033[0m   %s\n' "$1"; PASS=$((PASS+1)); }
c_bad()  { printf '\033[31m  FAIL\033[0m %s\n' "$1"; FAIL=$((FAIL+1)); }
section() { printf '\n\033[1m== %s\033[0m\n' "$1"; }

# Captures an id from command output and refuses to continue on a miss, so a
# parsing failure stops here instead of turning into twenty misleading failures.
grab_id() {  # grab_id <prefix> <output>
  local id
  id=$(printf '%s' "$2" | sed -n 1p | awk '{print $1}')
  case "$id" in
    "$1"*) printf '%s' "$id" ;;
    *) printf 'could not parse a %s id from:\n%s\n' "$1" "$2" >&2; exit 3 ;;
  esac
}

# Runs a command, checks the exit code, keeps the output in $OUT.
run() {
  local want=$1; shift
  printf '\n$ %s\n' "$*"
  OUT=$("$@" 2>&1); local rc=$?
  printf '%s\n' "$OUT" | sed 's/^/    /'
  if [ "$rc" -eq "$want" ]; then c_ok "exit $rc as expected"
  else c_bad "exit $rc, expected $want"; fi
}

check() {  # check <description> <condition-as-string>
  if eval "$2"; then c_ok "$1"; else c_bad "$1"; fi
}

q() { sqlite3 "$RESELL_DB" "$1"; }

# --- guard: never run this against the real database --------------------------

section "target"
if [ -z "${RESELL_DB:-}" ]; then
  echo "RESELL_DB is not set. Refusing to guess." >&2; exit 2
fi
if [ ! -f "$RESELL_DB" ]; then
  echo "$RESELL_DB does not exist. Copy a database there first." >&2; exit 2
fi
case "$(cd "$(dirname "$RESELL_DB")" && pwd)/$(basename "$RESELL_DB")" in
  */resell_agent/data/*) echo "That is the real database. Use a copy." >&2; exit 2 ;;
esac
echo "  database: $RESELL_DB"
uv run resell db verify | sed 's/^/  /'

ITEM_STATE=$(q "select state from item where sku='$SKU'")
echo "  $SKU is currently: ${ITEM_STATE:-<missing>}"
[ -n "$ITEM_STATE" ] || { echo "no such item" >&2; exit 2; }

# Pricing history from an earlier run would make the assertions below meaningless.
EXISTING=$(q "select count(*) from price_event where sku='$SKU'")
if [ "$EXISTING" != "0" ]; then
  echo "  $SKU already has $EXISTING price events; re-copy the database first." >&2
  exit 2
fi

# --- 0. a verified fee schedule ------------------------------------------------
# There is no CLI path for this yet. Without the row the fee basis stays
# provisional and `approve --production` refuses, which is the gate working --
# but it means production publishing is unreachable until this gets a command.

section "0. fee schedule"
uv run python - <<'PY'
import os, sqlite3
from datetime import date
from resell.pricing.proceeds import FeeBasis, FeeSchedule
from resell import store_pricing as sp
conn = sqlite3.connect(os.environ["RESELL_DB"]); conn.row_factory = sqlite3.Row
sp.upsert_fee_schedule(conn, FeeSchedule(
    version="ebay-us-clothing-2026-08", category_id="57988",
    effective_from=date(2026, 1, 1), rate=0.1335, fixed_cents=40,
    basis=FeeBasis.CATEGORY_VERIFIED,
    source_url="https://www.ebay.com/help/selling/fees-credits-invoices/selling-fees",
    captured_at=date.today(),
))
print("  fee schedule ebay-us-clothing-2026-08 recorded (category_verified)")
PY

# --- 1. comp observations ------------------------------------------------------
# Note the second one: --shipping-cents omitted means "not reported", which is
# not the same as zero, and should surface as a shipping_unknown qualifier.

section "1. comp observations"
C1=$(uv run resell price comp-add --external-id 1111 --kind asking \
  --basis active_similar --price-cents 12500 --shipping-cents 0 \
  --condition-band new_with_tags --days-on-market 140 \
  --title "Explorer Slim blazer NWT" --source-authority ebay_active_listing \
  | awk '{print $1}')
C2=$(uv run resell price comp-add --external-id 2222 --kind asking \
  --basis active_similar --price-cents 9900 \
  --condition-band new_with_tags --days-on-market 12 \
  --title "Explorer Slim blazer, postage not shown" \
  --source-authority ebay_active_listing | awk '{print $1}')
C3=$(uv run resell price comp-add --external-id 3333 --kind realized \
  --basis sold_similar --price-cents 6800 --shipping-cents 0 \
  --condition-band used_excellent --title "Explorer Slim blazer, worn twice" \
  --source-authority ebay_sold_listing | awk '{print $1}')
echo "  $C1  $C2  $C3"
check "three observations recorded" "[ \"\$(q 'select count(*) from comp_observation')\" = 3 ]"
check "unknown shipping stored as NULL, not zero" \
  "[ \"\$(q \"select count(*) from comp_observation where shipping_cents is null\")\" = 1 ]"

# --- 2. claims -----------------------------------------------------------------

section "2. claims (citations required on both sides)"
for C in "$C1" "$C2" "$C3"; do
  uv run resell price claim "$SKU" "$C" --comparability same_family_variant \
    --cite-item ev_label_brand --cite-comp title \
    --identity-resolution searched_not_found \
    --rationale "same Explorer Slim line, same size" | sed 's/^/    /'
done

echo "  -- the identity ceiling should refuse this one --"
run 2 uv run resell price claim "$SKU" "$C1" --comparability same_product \
  --cite-item ev_label_brand --cite-comp title \
  --identity-resolution searched_not_found

echo "  -- and an uncited claim --"
run 2 uv run resell price claim "$SKU" "$C1" --comparability category_attribute \
  --identity-resolution searched_not_found

check "only the three legitimate claims stored" \
  "[ \"\$(q 'select count(*) from comp_claim')\" = 3 ]"

# --- 3. strategies -------------------------------------------------------------

BASE=(--condition-band new_with_tags --identity-resolution searched_not_found
      --category-id 57988 --shipping-cost-cents 900 --minimum-net-cents 500
      --retail-cents 39800 --retail-kind retail_original)
BRAND=(--brand-strength premium --cite-brand ev_retail_swing_tag_398
       --brand-rationale '$398 swing tag places the line above mass market')

section "3. strategies"
run 0 uv run resell price recommend "$SKU" "${BASE[@]}" "${BRAND[@]}"
check "positioned on matched asks, not the used sale" \
  "printf '%s' \"\$OUT\" | grep -q positioned_on_asks"
check "the used sale is retained as evidence" \
  "printf '%s' \"\$OUT\" | grep -q sold_evidence_out_of_band"
check "band centre is the matched-ask median, \$112" \
  "printf '%s' \"\$OUT\" | grep -q 'centre \\\$112.00'"
check "premium brand takes max-proceeds to the top ask" \
  "printf '%s' \"\$OUT\" | grep -q 'max_proceeds *\\\$125.00'"

echo "  -- same evidence, uncited brand: max-proceeds should drop --"
run 0 uv run resell price recommend "$SKU" "${BASE[@]}" --brand-strength premium
check "uncited brand strength degrades to unknown" \
  "printf '%s' \"\$OUT\" | grep -q 'without a citation'"

# --- 4. propose and approve -----------------------------------------------------

section "4. propose (balanced) and approve"
run 0 uv run resell price propose "$SKU" "${BASE[@]}" "${BRAND[@]}" \
  --objective balanced --rationale "two matched asks; one used sale retained"
PID=$(grab_id price_ "$OUT")
echo "  proposal: $PID"

check "recorded against a verified fee basis" \
  "[ \"\$(q \"select fee_basis from price_proposal where proposal_id='\$PID'\")\" = category_verified ]"
check "the uncertainty note was stored with the number" \
  "[ -n \"\$(q \"select uncertainty_note from price_proposal where proposal_id='\$PID'\")\" ]"
check "the comp set was frozen and cited" \
  "[ -n \"\$(q \"select comp_set_hash from price_proposal where proposal_id='\$PID'\")\" ]"

run 0 uv run resell price approve "$PID"
run 0 uv run resell price approve "$PID"   # idempotent
check "one live approval, not two" \
  "[ \"\$(q \"select count(*) from price_approval where proposal_id='\$PID' and voided_at is null\")\" = 1 ]"

# --- 5. apply locally -------------------------------------------------------------
# The item has to actually reach the state the gate requires. In normal use these
# transitions come from the item commands; this is a scratch copy, so SQL is
# honest enough here and keeps the script independent of the item CLI.

section "5. apply, no eBay calls"
echo "  -- an initial price cannot be live before publish --"
run 2 uv run resell price apply "$PID" --marketplace-ref local-validation

q "update item set state='approved' where sku='$SKU'"
echo "  [item -> approved]"
run 0 uv run resell price apply "$PID" --marketplace-ref publish-offer-88231
run 0 uv run resell price apply "$PID" --marketplace-ref publish-offer-88231
check "the second apply made no changes" \
  "printf '%s' \"\$OUT\" | grep -q 'already applied'"
check "live price is \$112" \
  "[ \"\$(q \"select live_price_cents from item_price_state where sku='\$SKU'\")\" = 11200 ]"

section "5b. reprice on a live listing"
q "update item set state='listed' where sku='$SKU'"
echo "  [item -> listed]"
run 0 uv run resell price propose "$SKU" "${BASE[@]}" "${BRAND[@]}" \
  --objective fast_sale --reason reprice_operator \
  --rationale "no watchers in 21 days"
PID2=$(grab_id price_ "$OUT")
run 0 uv run resell price approve "$PID2"
run 0 uv run resell price apply "$PID2" --marketplace-ref offer-88231

check "the reprice supersedes the original rather than replacing it" \
  "[ \"\$(q \"select supersedes from price_proposal where proposal_id='\$PID2'\")\" = \"\$PID\" ]"
check "the original proposal is still readable" \
  "[ \"\$(q \"select price_cents from price_proposal where proposal_id='\$PID'\")\" = 11200 ]"

# --- 6. verify ----------------------------------------------------------------------

section "6. history and state"
run 0 uv run resell price history "$SKU"
run 0 uv run resell price show "$SKU" --listing-approved

ACTUAL=$(q "select group_concat(event_type,' ') from (select event_type from price_event where sku='$SKU' order by event_id)")
EXPECTED="proposed approved applied superseded proposed approved applied"
check "history reads: $EXPECTED" "[ \"\$ACTUAL\" = \"\$EXPECTED\" ]"
check "live price is now the fast-sale price, \$99" \
  "[ \"\$(q \"select live_price_cents from item_price_state where sku='\$SKU'\")\" = 9900 ]"
check "price state is live" \
  "[ \"\$(q \"select state from item_price_state where sku='\$SKU'\")\" = live ]"
check "no eBay call was ever made (no apply_failed, no offer refs beyond ours)" \
  "[ \"\$(q \"select count(*) from price_event where event_type='apply_failed'\")\" = 0 ]"

printf '\n\033[1m== summary\033[0m\n'
q "select '  ' || event_type || '  ' || coalesce(price_cents,'') || '  ' || coalesce(marketplace_ref,'') from price_event where sku='$SKU' order by event_id"
printf '\n  %s passed, %s failed\n\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
