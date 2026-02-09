"""Tests for redemption encoding and contract address constants."""

from src.execution.redeemer import (
    encode_redeem_positions,
    _function_selector,
    CTF_ADDRESS,
    NEG_RISK_ADAPTER_ADDRESS,
    USDC_ADDRESS,
)


class TestEncodeRedeemPositions:
    def test_output_starts_with_0x(self):
        result = encode_redeem_positions(
            collateral_token=USDC_ADDRESS,
            parent_collection_id=bytes(32),
            condition_id="ab" * 32,
            index_sets=[1, 2],
        )
        assert result.startswith("0x")

    def test_with_0x_prefix_condition_id(self):
        condition_id = "0x" + "ab" * 32
        result = encode_redeem_positions(
            collateral_token=USDC_ADDRESS,
            parent_collection_id=bytes(32),
            condition_id=condition_id,
            index_sets=[1, 2],
        )
        assert isinstance(result, str)
        assert len(result) > 10

    def test_without_0x_prefix_condition_id(self):
        condition_id = "ab" * 32
        result = encode_redeem_positions(
            collateral_token=USDC_ADDRESS,
            parent_collection_id=bytes(32),
            condition_id=condition_id,
            index_sets=[1, 2],
        )
        assert isinstance(result, str)
        assert len(result) > 10

    def test_both_formats_produce_same_encoding(self):
        cid = "ab" * 32
        r1 = encode_redeem_positions(
            collateral_token=USDC_ADDRESS,
            parent_collection_id=bytes(32),
            condition_id=cid,
            index_sets=[1, 2],
        )
        r2 = encode_redeem_positions(
            collateral_token=USDC_ADDRESS,
            parent_collection_id=bytes(32),
            condition_id="0x" + cid,
            index_sets=[1, 2],
        )
        assert r1 == r2

    def test_function_selector_is_4_bytes(self):
        result = encode_redeem_positions(
            collateral_token=USDC_ADDRESS,
            parent_collection_id=bytes(32),
            condition_id="ab" * 32,
            index_sets=[1, 2],
        )
        raw_bytes = bytes.fromhex(result[2:])
        assert len(raw_bytes) >= 4


class TestFunctionSelector:
    def test_selector_length(self):
        selector = _function_selector("redeemPositions(address,bytes32,bytes32,uint256[])")
        assert len(selector) == 4

    def test_selector_deterministic(self):
        s1 = _function_selector("redeemPositions(address,bytes32,bytes32,uint256[])")
        s2 = _function_selector("redeemPositions(address,bytes32,bytes32,uint256[])")
        assert s1 == s2


class TestContractAddresses:
    def test_ctf_address(self):
        assert CTF_ADDRESS == "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

    def test_neg_risk_adapter_address(self):
        assert NEG_RISK_ADAPTER_ADDRESS == "0xC5d563A36AE78145C45a50134d48A1215220f80a"

    def test_usdc_address(self):
        assert USDC_ADDRESS == "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
