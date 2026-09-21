"""Unit tests for Layer 1: Computation Engine."""

import pytest
import pandas as pd
import numpy as np

from src.computation import (
    compute_test_4_4,
    compute_test_5_1,
    compute_test_5_2,
    compute_test_6_1a,
    THRESHOLD_HIGH_VALUE,
)
from src.data_loader import EXCO_MEMBERS


@pytest.fixture
def sample_combined_df():
    """Create a sample combined DataFrame for testing."""
    return pd.DataFrame({
        "Employee": ["Shiner, Anthony"] * 5 + ["Ross, Felicity"] * 3,
        "Employee ID": ["E001"] * 5 + ["E002"] * 3,
        "Vendor": ["Vendor A"] * 3 + ["Vendor B"] * 2 + ["Vendor C"] * 3,
        "Transaction Date": pd.to_datetime([
            "2025-03-01", "2025-03-01", "2025-03-01",
            "2025-04-15", "2025-04-15",
            "2025-05-01", "2025-05-01", "2025-05-01",
        ]),
        "Expense Type": ["Travel"] * 8,
        "Expense Amount (reimbursement currency)": [
            2000, 2000, 2000,  # Split: 3x$2000 = $6000 > $5K
            3000, 3000,  # Split: 2x$3000 = $6000 > $5K
            1000, 1000, 1000,  # Not split: 3x$1000 = $3000 < $5K
        ],
        "Cross Charge Approver": ["Manager A"] * 8,
    })


@pytest.fixture
def sample_approval_df():
    """Create a sample approval aging DataFrame."""
    return pd.DataFrame({
        "Approver Name": [
            "Shiner, Anthony", "Shiner, Anthony", "Shiner, Anthony",
            "Ross, Felicity", "Ross, Felicity",
        ],
        "Employee Name": ["Staff A", "Staff B", "Staff C", "Staff D", "Staff E"],
        "Report Key": ["R1", "R2", "R3", "R4", "R5"],
        "Minutes": [0.5, 30, 0.3, 0.2, 0.1],  # 0.5 and 0.3 and 0.2 and 0.1 are instant
        "Report Viewed Receipts": ["Y", "Y", "N", "N", "N"],
        "Entry Viewed Receipts": ["N", "N", "N", "Y", "N"],
    })


class TestHighValueClaims:
    """Test 4.4: Reimbursement > $5K."""

    def test_flags_claims_over_5k(self, sample_combined_df):
        result = compute_test_4_4(sample_combined_df)
        # No individual claim is > $5K in our sample
        assert result["count"] == 0

    def test_flags_high_value_claim(self):
        df = pd.DataFrame({
            "Expense Amount (reimbursement currency)": [6000, 3000, 10000, 500],
        })
        result = compute_test_4_4(df)
        assert result["count"] == 2
        assert result["amount"] == 16000


class TestSplitClaims:
    """Test 5.1: Split Claims."""

    def test_detects_same_day_splits(self, sample_combined_df):
        result = compute_test_5_1(sample_combined_df)
        # Should detect the Vendor A group (3 claims, $6K)
        # and Vendor B group (2 claims, $6K)
        assert result["same_day"] >= 2  # At least the split pairs


class TestDuplicates:
    """Test 5.2: Duplicate Claims."""

    def test_detects_duplicates(self):
        df = pd.DataFrame({
            "Employee": ["Shiner, Anthony"] * 4,
            "Transaction Date": ["2025-01-01"] * 4,
            "Vendor": ["Vendor X", "Vendor X", "Vendor Y", "Vendor Y"],
            "Expense Amount (reimbursement currency)": [100, 100, 200, 200],
        })
        result = compute_test_5_2(df)
        assert result["count"] == 4  # All 4 are part of duplicate pairs


class TestApproverReview:
    """Test 6.1a: Approver Review Sufficiency."""

    def test_calculates_receipt_viewed(self, sample_approval_df):
        result = compute_test_6_1a(sample_approval_df)
        # Shiner: 2/3 viewed (Y in report), Ross: 1/2 viewed (Y in entry)
        metrics = result["approver_metrics"]
        assert len(metrics) == 2

        shiner = next(m for m in metrics if m["name"] == "Shiner, Anthony")
        assert shiner["reports"] == 3
        # 2 of 3 viewed = 66.7%
        assert abs(shiner["receipt_viewed_pct"] - 66.7) < 0.1

    def test_calculates_instant_approval(self, sample_approval_df):
        result = compute_test_6_1a(sample_approval_df)
        metrics = result["approver_metrics"]

        ross = next(m for m in metrics if m["name"] == "Ross, Felicity")
        # Both of Ross's approvals are < 1 minute = 100% instant
        assert ross["instant_pct"] == 100.0


class TestExcoMembers:
    """Validate ExCo member list."""

    def test_exco_count(self):
        assert len(EXCO_MEMBERS) == 11

    def test_exco_format(self):
        """All members should be in 'Surname, Firstname' format."""
        for member in EXCO_MEMBERS:
            assert "," in member, f"{member} is not in 'Surname, First' format"
