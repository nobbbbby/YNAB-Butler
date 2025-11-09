import pandas as pd

from importers.ingestion_engine import IngestionEngine, IngestionItem, SourceCallbacks


class DummyClient:
    def __init__(self, upload_result: bool = True) -> None:
        self.upload_result = upload_result
        self.upload_calls = []
        self.account_calls = 0

    def list_accounts(self, budget_id: str):
        self.account_calls += 1
        return [{'id': 'acc-1', 'name': 'Checking'}]

    def upload_transactions(self, transactions, budget_id: str) -> bool:
        self.upload_calls.append((budget_id, transactions))
        return self.upload_result


def test_ingestion_engine_batches_and_invokes_callbacks(monkeypatch):
    from importers import ingestion_engine as engine_module

    monkeypatch.setattr(
        engine_module,
        'convert_to_ynab_format',
        lambda df, default_account_id, name_map=None: [{'count': len(df)}],
    )
    client = DummyClient()
    engine = IngestionEngine(
        client,
        budgets=[{'id': 'b1', 'name': 'Primary'}],
        state_store=None,
        default_budget_id='b1',
        default_account_id='acc-1',
        force_budget_id='b1',
    )
    df_one = pd.DataFrame({'owner_name': ['alice'], 'value': [1]})
    df_two = pd.DataFrame({'owner_name': ['alice'], 'value': [2]})
    success_items = []
    callbacks = SourceCallbacks(on_success=lambda item: success_items.append(item.item_id))

    engine.add_items(
        [
            IngestionItem('item-1', 'file1.csv', df_one, 'alice', 'local'),
            IngestionItem('item-2', 'file2.csv', df_two, 'alice', 'email'),
        ],
        callbacks=callbacks,
    )
    result = engine.flush()

    assert result.all_succeeded is True
    assert client.account_calls == 1  # accounts fetched once due to caching
    assert len(client.upload_calls) == 1
    assert success_items == ['item-1', 'item-2']
    summary = engine.summary
    assert summary['alice']['prepared'] == 2
    assert summary['alice']['uploaded'] == 2


def test_record_source_warning_tracks_metadata():
    client = DummyClient()
    engine = IngestionEngine(
        client,
        budgets=[],
        state_store=None,
        default_budget_id=None,
        default_account_id=None,
        force_budget_id='b1',
    )
    engine.record_source_warning('fallback', 'file.csv', {'message_uids': {'123'}}, 'Parse failure')

    summary = engine.summary
    assert 'fallback' in summary
    entry = summary['fallback']
    assert entry['skipped'] == 1
    assert entry['warnings'] == ['Parse failure']
    assert entry['messages'] == {'123'}


def test_ingestion_engine_invokes_failure_callback(monkeypatch):
    from importers import ingestion_engine as engine_module

    monkeypatch.setattr(
        engine_module,
        'convert_to_ynab_format',
        lambda df, default_account_id, name_map=None: [{'count': len(df)}],
    )
    client = DummyClient(upload_result=False)
    engine = IngestionEngine(
        client,
        budgets=[{'id': 'b1', 'name': 'Primary'}],
        state_store=None,
        default_budget_id='b1',
        default_account_id='acc-1',
        force_budget_id='b1',
    )
    df = pd.DataFrame({'owner_name': ['bob'], 'value': [3]})
    failures = []
    callbacks = SourceCallbacks(on_failure=lambda item, reason: failures.append((item.item_id, reason)))

    engine.add_items([IngestionItem('item-3', 'file3.csv', df, 'bob', 'email')], callbacks=callbacks)
    result = engine.flush()

    assert result.all_succeeded is False
    assert len(result.failed_items) == 1
    assert failures and failures[0][0] == 'item-3'
    assert 'YNAB upload failed' in failures[0][1]
    summary = engine.summary
    assert summary['bob']['warnings']
