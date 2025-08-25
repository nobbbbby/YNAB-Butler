import ynab
import logging
import json
from typing import List, Any, Dict, Optional

class YNABClient:
    def __init__(self, api_key: str):
        self.configuration = ynab.Configuration(access_token=api_key)
        # Enable debug logging for requests and responses
        self.logger = logging.getLogger(__name__)
        self.logger.setLevel(logging.DEBUG)

    def _log_response(self, method: str, url: str, response: Any) -> None:
        """Log API response details at DEBUG level."""
        if not self.logger.isEnabledFor(logging.DEBUG):
            return
            
        try:
            response_data = {
                'method': method,
                'url': url,
                'status_code': getattr(response, 'status', None),
                'headers': dict(getattr(response, 'headers', {})),
            }
            
            # Try to get response data if available
            data = getattr(response, 'data', None)
            if data is not None:
                # Convert response data to dict for better logging
                response_data['data'] = data.to_dict() if hasattr(data, 'to_dict') else str(data)
                
            self.logger.debug('YNAB API Response: %s', 
                           json.dumps(response_data, indent=2, default=str))
        except Exception as e:
            self.logger.warning('Failed to log response: %s', str(e), exc_info=True)

    def _log_request(self, method: str, url: str, **kwargs) -> None:
        """Log API request details at DEBUG level."""
        if not self.logger.isEnabledFor(logging.DEBUG):
            return
            
        try:
            request_data = {
                'method': method,
                'url': url,
                'params': kwargs.get('query_params'),
                'headers': dict(kwargs.get('headers', {})),
            }
            
            # Handle request body if present
            if 'body' in kwargs:
                body = kwargs['body']
                if hasattr(body, 'to_dict'):
                    request_data['body'] = body.to_dict()
                else:
                    request_data['body'] = str(body)
                    
            self.logger.debug('YNAB API Request: %s', 
                           json.dumps(request_data, indent=2, default=str))
        except Exception as e:
            self.logger.warning('Failed to log request: %s', str(e), exc_info=True)

    def list_budgets(self) -> List[dict]:
        """Return a list of budgets as dicts with at least id and name."""
        try:
            with ynab.ApiClient(self.configuration) as api_client:
                budgets_api = ynab.BudgetsApi(api_client)
                self._log_request('GET', '/budgets')
                resp = budgets_api.get_budgets()
                self._log_response('GET', '/budgets', resp)
                data = getattr(resp, "data", None)
                budgets_list = getattr(data, "budgets", []) if data is not None else []
                budgets = [{"id": b.id, "name": b.name} for b in budgets_list]
                self.logger.debug('Extracted %d budgets', len(budgets))
                return budgets
        except Exception as e:
            self.logger.error(f"Error fetching budgets from YNAB: {e}", exc_info=True)
            return []

    def list_accounts(self, budget_id: str) -> List[dict]:
        """Return accounts in a budget as dicts with id and name."""
        try:
            with ynab.ApiClient(self.configuration) as api_client:
                accounts_api = ynab.AccountsApi(api_client)
                endpoint = f'/budgets/{budget_id}/accounts'
                self._log_request('GET', endpoint, query_params={'budget_id': budget_id})
                resp = accounts_api.get_accounts(budget_id)
                self._log_response('GET', endpoint, resp)
                data = getattr(resp, "data", None)
                accounts_list = getattr(data, "accounts", []) if data is not None else []
                accounts = [{"id": a.id, "name": a.name, "type": a.type} for a in accounts_list]
                self.logger.debug('Extracted %d accounts for budget %s', len(accounts), budget_id)
                return accounts
        except Exception as e:
            self.logger.error(f"Error fetching accounts from YNAB: {e}", exc_info=True)
            return []

    def upload_transactions(self, transactions: List[dict], budget_id: str) -> bool:
        """Upload transactions to YNAB."""
        if not transactions:
            self.logger.warning("No transactions to upload")
            return False
            
        try:
            with ynab.ApiClient(self.configuration) as api_client:
                transactions_api = ynab.TransactionsApi(api_client)
                endpoint = f'/budgets/{budget_id}/transactions'
                
                # Log the request details
                request_data = {"transactions": transactions}
                self._log_request('POST', endpoint, body=request_data)
                
                # Create new transactions
                response = transactions_api.create_transaction(
                    budget_id,
                    request_data
                )
                
                # Log the response details
                self._log_response('POST', endpoint, response)
                
                # Log summary of the upload
                imported = getattr(getattr(response, 'data', None), 'transaction_ids', None)
                if imported:
                    self.logger.info(
                        'Successfully uploaded %d/%d transactions to YNAB', 
                        len(imported), 
                        len(transactions)
                    )
                    if len(imported) < len(transactions):
                        self.logger.warning(
                            'Some transactions were not imported. Check YNAB for details.'
                        )
                return True
                
        except Exception as e:
            self.logger.error(
                'Error uploading to YNAB: %s', 
                str(e), 
                exc_info=self.logger.isEnabledFor(logging.DEBUG)
            )
            return False
