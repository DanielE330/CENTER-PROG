"""Общая детерминированная модель данных для имитаторов 1С/ЭДО/банка (только данные, без HTTP)."""
from .model import (Obligation, Model, company_exists, company_info, dataset_dir, guid, model,
                    operations, opening_balance, today)
from .chaos import delay_and_should_fail

__all__ = ["Obligation", "Model", "company_exists", "company_info", "dataset_dir", "guid", "model",
           "operations", "opening_balance", "today", "delay_and_should_fail"]
