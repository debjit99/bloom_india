"""
bloom_india/data/process/xbrl_parse.py
=======================================
Parses raw XBRL JSON cache into a comprehensive DataFrame.
Captures ALL available tags — P&L, balance sheet, cash flow,
segment data, ratios, banking-specific fields, and metadata.

Only shows fields that are actually present — no empty columns.
"""

import json
import pandas as pd
import numpy as np
from pathlib import Path
from typing import Optional

from bloom_india.config import CONFIG


# ── Unit helpers ──────────────────────────────────────────────────────────────

def _cr(v) -> Optional[float]:
    if v is None: return None
    try:
        f = float(str(v).replace(",", "").strip())
        return round(f / 1e7, 2) if abs(f) > 1e6 else f
    except: return None

def _fl(v) -> Optional[float]:
    if v is None: return None
    try: return float(str(v).replace(",", "").strip())
    except: return None

def _get_num(raw: dict, *keys) -> Optional[float]:
    for k in keys:
        v = raw.get(k)
        if v is not None:
            try:
                f = float(str(v).replace(",", "").strip())
                if f != 0.0: return f
            except: pass
    return None

def _get_str(raw: dict, *keys) -> Optional[str]:
    for k in keys:
        v = raw.get(k)
        if v and str(v).strip():
            return str(v).strip()
    return None


# ── Period helpers ────────────────────────────────────────────────────────────

def _period_days(raw: dict) -> int:
    try:
        s = pd.to_datetime(raw.get("startDate", ""))
        e = pd.to_datetime(raw.get("DateOfEndOfReportingPeriod", "") or raw.get("endDate", ""))
        return int((e - s).days)
    except: return 0

def _parse_period(period_end: str):
    try:
        d  = pd.to_datetime(period_end, dayfirst=True)
        fy = d.year if d.month < 4 else d.year + 1
        q  = {3:"Q4", 6:"Q1", 9:"Q2", 12:"Q3"}.get(d.month, "Q?")
        return q, fy
    except: return "Q?", 0


# ── Single file parser ────────────────────────────────────────────────────────

def parse_one(json_path: str) -> Optional[dict]:
    with open(json_path) as f:
        rec = json.load(f)

    raw = rec.get("raw_xbrl", {})
    if not raw or "_download_error" in raw or "_parse_error" in raw:
        return None

    days = _period_days(raw)
    if days > 105 or (0 < days < 60):
        return None

    period_end = raw.get("DateOfEndOfReportingPeriod") or rec.get("period_end", "")
    if not period_end:
        return None

    quarter, fy = _parse_period(period_end)
    if not fy:
        return None

    nature       = raw.get("NatureOfReportStandaloneConsolidated", "").lower()
    consolidated = "consolidated" in nature

    r = {}

    # ── Identity ──────────────────────────────────────────────────────────────
    r["symbol"]        = rec.get("symbol", "")
    r["period_end"]    = period_end
    r["period_days"]   = days
    r["quarter"]       = quarter
    r["fy"]            = fy
    r["quarter_label"] = f"{quarter}_FY{fy}"
    r["consolidated"]  = consolidated

    # ── Metadata ──────────────────────────────────────────────────────────────
    r["board_meeting_date"] = _get_str(raw, "DateOfBoardMeetingWhenFinancialResultsWereApproved")
    r["result_type"]        = _get_str(raw, "ResultType")
    r["audited"]            = _get_str(raw, "WhetherResultsAreAuditedOrUnaudited")
    r["face_value"]         = _fl(_get_num(raw, "FaceValueOfEquityShareCapital"))
    r["multi_segment"]      = _get_str(raw, "IsCompanyReportingMultisegmentOrSingleSegment")
    r["isin"]               = _get_str(raw, "ISIN")

    # ── Non-banking P&L ───────────────────────────────────────────────────────
    r["revenue"]             = _cr(_get_num(raw, "RevenueFromOperations", "InterestEarned", "IncomeFromOperations", "NetSales"))
    r["other_income"]        = _cr(_get_num(raw, "OtherIncome"))
    r["total_income"]        = _cr(_get_num(raw, "Income"))
    r["cost_of_materials"]   = _cr(_get_num(raw, "CostOfMaterialsConsumed"))
    r["purchases_stock"]     = _cr(_get_num(raw, "PurchasesOfStockInTrade"))
    r["inventory_change"]    = _cr(_get_num(raw, "ChangesInInventoriesOfFinishedGoodsWorkInProgressAndStockInTrade"))
    r["employee_cost"]       = _cr(_get_num(raw, "EmployeeBenefitExpense", "EmployeesCost"))
    r["finance_costs"]       = _cr(_get_num(raw, "FinanceCosts", "InterestExpended"))
    r["depreciation"]        = _cr(_get_num(raw, "DepreciationDepletionAndAmortisationExpense"))
    r["other_expenses"]      = _cr(_get_num(raw, "OtherExpenses"))
    r["total_expenses"]      = _cr(_get_num(raw, "Expenses"))
    r["ebitda"]              = _cr(_get_num(raw, "ProfitBeforeExceptionalItemsAndTax"))
    r["exceptional_items"]   = _cr(_get_num(raw, "ExceptionalItemsBeforeTax", "ExceptionalItems"))
    r["profit_before_tax"]   = _cr(_get_num(raw, "ProfitBeforeTax", "ProfitLossFromOrdinaryActivitiesBeforeTax"))
    r["current_tax"]         = _cr(_get_num(raw, "CurrentTax"))
    r["deferred_tax"]        = _cr(_get_num(raw, "DeferredTax"))
    r["tax"]                 = _cr(_get_num(raw, "TaxExpense"))
    r["pat_continuing"]      = _cr(_get_num(raw, "ProfitLossForPeriodFromContinuingOperations", "ProfitLossFromOrdinaryActivitiesAfterTax"))
    r["pat_discontinued"]    = _cr(_get_num(raw, "ProfitLossFromDiscontinuedOperationsAfterTax"))
    r["pat"]                 = _cr(_get_num(raw, "ProfitLossForPeriod", "ProfitLossForThePeriod"))
    r["other_comprehensive_income"] = _cr(_get_num(raw, "OtherComprehensiveIncomeNetOfTaxes", "OtherComprehensiveIncome"))
    r["total_comprehensive_income"] = _cr(_get_num(raw, "ComprehensiveIncomeForThePeriod"))
    r["pat_owners"]          = _cr(_get_num(raw, "ProfitOrLossAttributableToOwnersOfParent", "ComprehensiveIncomeForThePeriodAttributableToOwnersOfParent"))
    r["pat_minority"]        = _cr(_get_num(raw, "ProfitOrLossAttributableToNonControllingInterests", "ProfitLossOfMinorityInterest"))
    r["share_of_associates"] = _cr(_get_num(raw, "ShareOfProfitLossOfAssociatesAndJointVenturesAccountedForUsingEquityMethod", "ShareOfProfitLossOfAssociates"))

    # ── EPS ───────────────────────────────────────────────────────────────────
    r["eps_basic"]            = _fl(_get_num(raw, "BasicEarningsPerShareBeforeExtraordinaryItems", "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations", "BasicEarningsLossPerShareFromContinuingOperations"))
    r["eps_diluted"]          = _fl(_get_num(raw, "DilutedEarningsPerShareBeforeExtraordinaryItems", "DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations", "DilutedEarningsLossPerShareFromContinuingOperations"))
    r["eps_basic_continuing"] = _fl(_get_num(raw, "BasicEarningsLossPerShareFromContinuingOperations"))
    r["eps_basic_discontinued"]= _fl(_get_num(raw, "BasicEarningsLossPerShareFromDiscontinuedOperations"))

    # ── Banking P&L ───────────────────────────────────────────────────────────
    r["interest_earned"]       = _cr(_get_num(raw, "InterestEarned"))
    r["interest_on_advances"]  = _cr(_get_num(raw, "InterestOrDiscountOnAdvancesOrBills"))
    r["income_on_investments"]  = _cr(_get_num(raw, "RevenueOnInvestments"))
    r["interest_on_rbi"]        = _cr(_get_num(raw, "InterestOnBalancesWithReserveBankOfIndiaAndOtherInterBankFunds"))
    r["other_interest"]         = _cr(_get_num(raw, "OtherInterest"))
    r["interest_expended"]      = _cr(_get_num(raw, "InterestExpended", "FinanceCosts"))
    r["operating_expenses_bank"]= _cr(_get_num(raw, "OperatingExpenses"))
    r["expenditure_excl_prov"]  = _cr(_get_num(raw, "ExpenditureExcludingProvisionsAndContingencies"))
    r["operating_profit_bank"]  = _cr(_get_num(raw, "OperatingProfitBeforeProvisionAndContingencies"))
    r["provisions_bank"]        = _cr(_get_num(raw, "ProvisionsOtherThanTaxAndContingencies"))
    r["dividend_income"]        = _cr(_get_num(raw, "DividendIncome"))
    r["fees_commission"]        = _cr(_get_num(raw, "FeesAndCommissionIncome"))
    r["net_gain_fair_value"]    = _cr(_get_num(raw, "NetGainOnFairValueChanges"))
    r["impairment_fin_inst"]    = _cr(_get_num(raw, "ImpairmentOnFinancialInstruments"))
    r["other_rev_from_ops"]     = _cr(_get_num(raw, "OtherRevenueFromOperations"))

    # ── Banking ratios ────────────────────────────────────────────────────────
    r["npa_gross_cr"]    = _cr(_get_num(raw, "GrossNonPerformingAssets"))
    r["npa_net_cr"]      = _cr(_get_num(raw, "NonPerformingAssets"))
    r["npa_pct_gross"]   = _fl(_get_num(raw, "PercentageOfGrossNpa"))
    r["npa_pct_net"]     = _fl(_get_num(raw, "PercentageOfNpa"))
    r["car"]             = _fl(_get_num(raw, "CET1Ratio"))
    r["tier1_additional"]= _fl(_get_num(raw, "AdditionalTier1Ratio"))
    r["roa"]             = _fl(_get_num(raw, "ReturnOnAssets"))
    r["pct_govt_holding"]= _fl(_get_num(raw, "PercentageOfShareHeldByGovernmentOfIndia"))

    # ── Capital ───────────────────────────────────────────────────────────────
    r["equity_capital"]   = _cr(_get_num(raw, "PaidUpValueOfEquityShareCapital", "EquityShareCapital", "Capital"))
    r["reserves_surplus"] = _cr(_get_num(raw, "OtherEquity", "ReservesAndSurplus", "ReserveExcludingRevaluationReserves"))
    r["equity_total"]     = _cr(_get_num(raw, "Equity"))
    r["equity_owners"]    = _cr(_get_num(raw, "EquityAttributableToOwnersOfParent"))
    r["minority_interest"]= _cr(_get_num(raw, "NonControllingInterest"))

    # ── Balance sheet — assets ────────────────────────────────────────────────
    r["total_assets"]          = _cr(_get_num(raw, "Assets", "EquityAndLiabilities", "CapitalAndLiabilities"))
    r["noncurrent_assets"]     = _cr(_get_num(raw, "NoncurrentAssets"))
    r["current_assets"]        = _cr(_get_num(raw, "CurrentAssets"))
    r["ppe"]                   = _cr(_get_num(raw, "PropertyPlantAndEquipment"))
    r["capwip"]                = _cr(_get_num(raw, "CapitalWorkInProgress"))
    r["goodwill"]              = _cr(_get_num(raw, "Goodwill"))
    r["intangibles"]           = _cr(_get_num(raw, "OtherIntangibleAssets"))
    r["intangibles_wip"]       = _cr(_get_num(raw, "IntangibleAssetsUnderDevelopment"))
    r["noncurrent_investments"]= _cr(_get_num(raw, "NoncurrentInvestments", "Investments"))
    r["current_investments"]   = _cr(_get_num(raw, "CurrentInvestments"))
    r["inventories"]           = _cr(_get_num(raw, "Inventories"))
    r["trade_receivables"]     = _cr(_get_num(raw, "TradeReceivablesCurrent", "TradeReceivables"))
    r["cash_equivalents"]      = _cr(_get_num(raw, "CashAndCashEquivalents", "CashAndBalancesWithReserveBankOfIndia"))
    r["bank_balances"]         = _cr(_get_num(raw, "BankBalanceOtherThanCashAndCashEquivalents", "BalancesWithBanksAndMoneyAtCallAndShortNotice"))
    r["loans_assets"]          = _cr(_get_num(raw, "LoansNoncurrent", "LoansCurrent", "Loans", "Advances"))
    r["other_financial_assets"]= _cr(_get_num(raw, "OtherNoncurrentFinancialAssets", "OtherCurrentFinancialAssets"))
    r["other_current_assets"]  = _cr(_get_num(raw, "OtherCurrentAssets"))
    r["other_noncurrent_assets"]= _cr(_get_num(raw, "OtherNoncurrentAssets"))
    r["deferred_tax_assets"]   = _cr(_get_num(raw, "DeferredTaxAssetsNet"))
    r["investment_property"]   = _cr(_get_num(raw, "InvestmentProperty"))
    r["investments_associates"]= _cr(_get_num(raw, "InvestmentsAccountedForUsingEquityMethod"))
    r["financial_assets_bank"] = _cr(_get_num(raw, "FinanicalAssets"))

    # ── Balance sheet — liabilities ───────────────────────────────────────────
    r["total_liabilities"]     = _cr(_get_num(raw, "Liabilities"))
    r["noncurrent_liabilities"]= _cr(_get_num(raw, "NoncurrentLiabilities"))
    r["current_liabilities"]   = _cr(_get_num(raw, "CurrentLiabilities"))
    r["borrowings_noncurrent"] = _cr(_get_num(raw, "BorrowingsNoncurrent"))
    r["borrowings_current"]    = _cr(_get_num(raw, "BorrowingsCurrent", "Borrowings"))
    r["trade_payables"]        = _cr(_get_num(raw, "TradePayablesCurrent"))
    r["other_current_liab"]    = _cr(_get_num(raw, "OtherCurrentLiabilities"))
    r["other_noncurrent_liab"] = _cr(_get_num(raw, "OtherNoncurrentLiabilities"))
    r["provisions_current"]    = _cr(_get_num(raw, "ProvisionsCurrent"))
    r["provisions_noncurrent"] = _cr(_get_num(raw, "ProvisionsNoncurrent"))
    r["deferred_tax_liab"]     = _cr(_get_num(raw, "DeferredTaxLiabilitiesNet"))
    r["current_tax_liab"]      = _cr(_get_num(raw, "CurrentTaxLiabilities"))
    r["deposits_bank"]         = _cr(_get_num(raw, "Deposits"))
    r["debt_securities"]       = _cr(_get_num(raw, "DebtSecurities"))
    r["subordinated_liab"]     = _cr(_get_num(raw, "SubordinatedLiabilities"))
    r["other_fin_liab"]        = _cr(_get_num(raw, "OtherCurrentFinancialLiabilities", "OtherFinancialLiabilities"))
    r["financial_liab_bank"]   = _cr(_get_num(raw, "FinancialLiabilities"))
    r["other_liab_provisions"] = _cr(_get_num(raw, "OtherLiabilitiesAndProvisions"))

    # ── Cash flow ─────────────────────────────────────────────────────────────
    r["cfo"]                  = _cr(_get_num(raw, "CashFlowsFromUsedInOperatingActivities"))
    r["cfi"]                  = _cr(_get_num(raw, "CashFlowsFromUsedInInvestingActivities"))
    r["cff"]                  = _cr(_get_num(raw, "CashFlowsFromUsedInFinancingActivities"))
    r["capex"]                = _cr(_get_num(raw, "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities", "PurchaseOfTangibleAssetsClassifiedAsInvestingActivities"))
    r["cash_end"]             = _cr(_get_num(raw, "CashAndCashEquivalentsCashFlowStatement"))
    r["net_change_cash"]      = _cr(_get_num(raw, "IncreaseDecreaseInCashAndCashEquivalents"))
    r["interest_paid_cf"]     = _cr(_get_num(raw, "InterestPaidClassifiedAsFinancingActivities", "InterestPaidClassifiedAsOperatingActivities"))
    r["tax_paid_cf"]          = _cr(_get_num(raw, "IncomeTaxesPaidRefundClassifiedAsOperatingActivities"))
    r["dividends_paid"]       = _cr(_get_num(raw, "DividendsPaidClassifiedAsFinancingActivities"))
    r["proceeds_borrowings"]  = _cr(_get_num(raw, "ProceedsFromBorrowingsClassifiedAsFinancingActivities"))
    r["repayments_borrowings"]= _cr(_get_num(raw, "RepaymentsOfBorrowingsClassifiedAsFinancingActivities"))
    r["free_cash_flow"]       = None  # computed later: cfo - capex

    # ── Financial ratios ──────────────────────────────────────────────────────
    r["debt_equity_ratio"]    = _fl(_get_num(raw, "DebtEquityRatio"))
    r["debt_service_ratio"]   = _fl(_get_num(raw, "DebtServiceCoverageRatio"))
    r["interest_coverage"]    = _fl(_get_num(raw, "InterestServiceCoverageRatio"))

    # ── Segment ───────────────────────────────────────────────────────────────
    r["segment_revenue"]      = _cr(_get_num(raw, "SegmentRevenue"))
    r["segment_revenue_ops"]  = _cr(_get_num(raw, "SegmentRevenueFromOperations"))
    r["segment_profit_bt"]    = _cr(_get_num(raw, "SegmentProfitBeforeTax"))
    r["segment_assets"]       = _cr(_get_num(raw, "SegmentAssets"))
    r["segment_liabilities"]  = _cr(_get_num(raw, "SegmentLiabilities"))
    r["inter_segment_rev"]    = _cr(_get_num(raw, "InterSegmentRevenue"))
    r["unallocable_assets"]   = _cr(_get_num(raw, "UnAllocableAssets"))
    r["unallocable_liab"]     = _cr(_get_num(raw, "UnAllocableLiabilities"))

    return r


# ── Full cache parser ─────────────────────────────────────────────────────────

def parse_xbrl_cache(
    xbrl_dir: Optional[str] = None,
    symbols:  Optional[list] = None,
    verbose:  bool = True,
) -> pd.DataFrame:
    """
    Parse all cached XBRL JSONs → comprehensive DataFrame.
    Drops columns that are entirely null.
    """
    base = Path(xbrl_dir) if xbrl_dir else Path(CONFIG.storage.xbrl_dir)

    if not base.exists():
        raise FileNotFoundError(
            f"XBRL cache not found: {base}\n"
            "Run: python -m bloom_india.scripts.build_fundamental_db --fetch"
        )

    rows     = []
    sym_dirs = sorted(d for d in base.iterdir() if d.is_dir())
    if symbols:
        sym_dirs = [d for d in sym_dirs if d.name.upper() in [s.upper() for s in symbols]]

    for sym_dir in sym_dirs:
        files = sorted(sym_dir.glob("*.json"))
        if verbose:
            print(f"  {sym_dir.name:<15} {len(files):>3} files")
        for jf in files:
            row = parse_one(str(jf))
            if row is not None:
                rows.append(row)

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df["period_end"] = pd.to_datetime(df["period_end"], errors="coerce", dayfirst=True)

    # Deduplicate: consolidated preferred, then most complete
    num_cols = df.select_dtypes(include=[np.number]).columns
    df["_nulls"] = df[num_cols].isnull().sum(axis=1)
    df = df.sort_values(
        ["symbol","quarter_label","consolidated","_nulls"],
        ascending=[True,True,False,True],
    )
    df = df.drop_duplicates(subset=["symbol","quarter_label"], keep="first")
    df = df.drop(columns=["_nulls"])

    # Compute derived fields
    if "cfo" in df.columns and "capex" in df.columns:
        df["free_cash_flow"] = df["cfo"] - df["capex"].abs()
    if "interest_earned" in df.columns and "interest_expended" in df.columns:
        df["nii"] = df["interest_earned"] - df["interest_expended"]

    # Drop columns that are entirely null
    before = len(df.columns)
    df = df.dropna(axis=1, how="all")
    after  = len(df.columns)

    df = df.sort_values(["symbol","period_end"]).reset_index(drop=True)

    if verbose:
        print(f"\n  Parsed  : {len(df)} rows, {df['symbol'].nunique()} symbols")
        print(f"  Columns : {after} (dropped {before-after} empty columns)")
        print(f"  Dates   : {df['period_end'].min().date()} → {df['period_end'].max().date()}")

    return df