import pathlib
ROOT=pathlib.Path(__file__).resolve().parents[1]
def test_kotak_uses_canonical_exchange_segments():
    s=(ROOT/"backend/app/brokers.py").read_text()
    for x in ("nse_fo","bse_fo","mcx_fo"): assert x in s
    assert "o['exchange'].lower()" not in s
def test_multi_venue_stream_segments_are_explicit():
    s=(ROOT/"backend/app/broker_streams.py").read_text()
    for x in ("NSE_FO","BSE_FO","MCX_FO","bse_fo","mcx_fo"): assert x in s
