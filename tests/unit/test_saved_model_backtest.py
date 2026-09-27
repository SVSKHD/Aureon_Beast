from types import SimpleNamespace
import pytest
import aureon.services.saved_model_backtest as subject

class Models:
    def __init__(self, model): self.model=model
    def get_model(self, model_id): return self.model if model_id == self.model.model_id else None

class Memory:
    def canonical_between(self, symbol, start, end): return [object(), object(), object()]

def test_rejected_model_is_backtestable_and_not_mutated(monkeypatch):
    model=SimpleNamespace(model_id="rejected_1", symbol="XAUUSD", status="rejected", algorithm="boosted_stumps_v1")
    rows=[
      {"probabilities":{"clean_10":.8},"actual":{"clean_10":True,"reach_5":True,"reach_10":True,"reach_20":False,"reach_30":False,"reach_40":False},"max_adverse_move":2.0,"max_favourable_move":12.0},
      {"probabilities":{"clean_10":.7},"actual":{"clean_10":False,"reach_5":True,"reach_10":False,"reach_20":False,"reach_30":False,"reach_40":False},"max_adverse_move":8.0,"max_favourable_move":6.0},
      {"probabilities":{"clean_10":.2},"actual":{"clean_10":True,"reach_5":True,"reach_10":True,"reach_20":True,"reach_30":False,"reach_40":False},"max_adverse_move":1.0,"max_favourable_move":22.0},
    ]
    monkeypatch.setattr(subject,"evaluate_v1_artifact",lambda m,e:{"samples":3,"scored_rows":rows,"metrics":{"clean_10":{"precision":.5}}})
    report=subject.backtest_saved_model(models=Models(model),training_memory=Memory(),symbol="XAUUSD",model_id="rejected_1",
      start_market_date="2026-01-01",end_market_date="2026-01-31",usd_per_move=25)
    assert report["model_status"] == "rejected"
    assert model.status == "rejected"
    assert report["trades"] == 2
    assert report["wins"] == 1 and report["losses"] == 1
    assert report["net_move"] == pytest.approx(3)
    assert report["net_pnl_usd"] == pytest.approx(75)
    assert report["max_drawdown_move"] == pytest.approx(7)

def test_unknown_model_fails_clearly():
    model=SimpleNamespace(model_id="known",symbol="XAUUSD",status="champion",algorithm="logistic_regression_v1")
    with pytest.raises(ValueError, match="model not found"):
        subject.backtest_saved_model(models=Models(model),training_memory=Memory(),symbol="XAUUSD",model_id="missing",
          start_market_date="2026-01-01",end_market_date="2026-01-31")
