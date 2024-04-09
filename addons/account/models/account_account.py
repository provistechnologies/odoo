from bisect import bisect_left
from collections import defaultdict
import itertools
import re

from odoo import api, fields, models, _, Command
from odoo.osv import expression
from odoo.exceptions import UserError, ValidationError, RedirectWarning
from odoo.tools import SQL, Query
from odoo.addons.base.models.ir_property import TYPE2FIELD


ACCOUNT_REGEX = re.compile(r'(?:(\S*\d+\S*))?(.*)')
ACCOUNT_CODE_REGEX = re.compile(r'^[A-Za-z0-9.]+$')
ACCOUNT_CODE_NUMBER_REGEX = re.compile(r'(.*?)(\d*)(\D*?)$')


class AccountAccount(models.Model):
    _name = "account.account"
    _inherit = ['mail.thread']
    _description = "Account"
    _order = "code"
    _check_company_auto = True
    _check_company_domain = models.check_companies_domain_parent_of

    @api.constrains('account_type', 'reconcile')
    def _check_reconcile(self):
        for account in self:
            if account.account_type in ('asset_receivable', 'liability_payable') and not account.reconcile:
                raise ValidationError(_('You cannot have a receivable/payable account that is not reconcilable. (account code: %s)', account.code))

    @api.constrains('account_type')
    def _check_account_type_unique_current_year_earning(self):
        result = self._read_group(
            domain=[('account_type', '=', 'equity_unaffected')],
            groupby=['company_ids'],
            aggregates=['id:recordset'],
            having=[('__count', '>', 1)],
        )
        for _company, account_unaffected_earnings in result:
            raise ValidationError(_('You cannot have more than one account with "Current Year Earnings" as type. (accounts: %s)', [a.code for a in account_unaffected_earnings]))

    name = fields.Char(string="Account Name", required=True, index='trigram', tracking=True, translate=True)
    currency_id = fields.Many2one('res.currency', string='Account Currency', tracking=True,
        help="Forces all journal items in this account to have a specific currency (i.e. bank journals). If no currency is set, entries can use any currency.")
    company_currency_id = fields.Many2one('res.currency', compute='_compute_company_currency_id')
    company_fiscal_country_code = fields.Char(compute='_compute_company_fiscal_country_code')
    code = fields.Char(string="Code", size=64, tracking=True, compute='_compute_code', search='_search_code', inverse='_inverse_code')
    deprecated = fields.Boolean(default=False, tracking=True)
    used = fields.Boolean(compute='_compute_used', search='_search_used')
    account_type = fields.Selection(
        selection=[
            ("asset_receivable", "Receivable"),
            ("asset_cash", "Bank and Cash"),
            ("asset_current", "Current Assets"),
            ("asset_non_current", "Non-current Assets"),
            ("asset_prepayments", "Prepayments"),
            ("asset_fixed", "Fixed Assets"),
            ("liability_payable", "Payable"),
            ("liability_credit_card", "Credit Card"),
            ("liability_current", "Current Liabilities"),
            ("liability_non_current", "Non-current Liabilities"),
            ("equity", "Equity"),
            ("equity_unaffected", "Current Year Earnings"),
            ("income", "Income"),
            ("income_other", "Other Income"),
            ("expense", "Expenses"),
            ("expense_depreciation", "Depreciation"),
            ("expense_direct_cost", "Cost of Revenue"),
            ("off_balance", "Off-Balance Sheet"),
        ],
        string="Type", tracking=True,
        required=True,
        compute='_compute_account_type', store=True, readonly=False, precompute=True, index=True,
        help="Account Type is used for information purpose, to generate country-specific legal reports, and set the rules to close a fiscal year and generate opening entries."
    )
    include_initial_balance = fields.Boolean(string="Bring Accounts Balance Forward",
        help="Used in reports to know if we should consider journal items from the beginning of time instead of from the fiscal year only. Account types that should be reset to zero at each new fiscal year (like expenses, revenue..) should not have this option set.",
        compute="_compute_include_initial_balance",
        search="_search_include_initial_balance",
    )
    internal_group = fields.Selection(
        selection=[
            ('equity', 'Equity'),
            ('asset', 'Asset'),
            ('liability', 'Liability'),
            ('income', 'Income'),
            ('expense', 'Expense'),
            ('off', 'Off Balance'),
        ],
        string="Internal Group",
        compute="_compute_internal_group",
        search='_search_internal_group',
    )
    reconcile = fields.Boolean(string='Allow Reconciliation', tracking=True,
        compute='_compute_reconcile', store=True, readonly=False, precompute=True,
        help="Check this box if this account allows invoices & payments matching of journal items.")
    tax_ids = fields.Many2many('account.tax', 'account_account_tax_default_rel',
        'account_id', 'tax_id', string='Default Taxes',
        check_company=True,
        context={'append_type_to_tax_name': True})
    note = fields.Text('Internal Notes', tracking=True)
    company_ids = fields.Many2many('res.company', string='Companies', required=True, readonly=False,
        default=lambda self: self.env.company)
    code_mapping_ids = fields.One2many(comodel_name='account.code.mapping', inverse_name='account_id')
    tag_ids = fields.Many2many(
        comodel_name='account.account.tag',
        relation='account_account_account_tag',
        compute='_compute_account_tags', readonly=False, store=True, precompute=True,
        string='Tags',
        help="Optional tags you may want to assign for custom reporting",
        ondelete='restrict',
        tracking=True,
    )
    group_id = fields.Many2one('account.group', compute='_compute_account_group',
                               help="Account prefixes can determine account groups.")
    root_id = fields.Many2one('account.root', compute='_compute_account_root', search='_search_account_root')
    allowed_journal_ids = fields.Many2many(
        'account.journal',
        string="Allowed Journals",
        help="Define in which journals this account can be used. If empty, can be used in all journals.",
        check_company=True,
    )
    opening_debit = fields.Monetary(string="Opening Debit", compute='_compute_opening_debit_credit', inverse='_set_opening_debit', currency_field='company_currency_id')
    opening_credit = fields.Monetary(string="Opening Credit", compute='_compute_opening_debit_credit', inverse='_set_opening_credit', currency_field='company_currency_id')
    opening_balance = fields.Monetary(string="Opening Balance", compute='_compute_opening_debit_credit', inverse='_set_opening_balance', currency_field='company_currency_id')

    current_balance = fields.Float(compute='_compute_current_balance')
    related_taxes_amount = fields.Integer(compute='_compute_related_taxes_amount')

    non_trade = fields.Boolean(default=False,
                               help="If set, this account will belong to Non Trade Receivable/Payable in reports and filters.\n"
                                    "If not, this account will belong to Trade Receivable/Payable in reports and filters.")

    def _field_to_sql(self, alias: str, fname: str, query: (Query | None) = None, flush: bool = True) -> SQL:
        if fname == 'internal_group':
            return SQL("split_part(account_account.account_type, '_', 1)", to_flush=self._fields['account_type'])
        if fname == 'code':
            field = self._fields.get(fname)

            company_dependent_field_alias = query.make_alias(alias, f'{field.name}_company_dependent')
            query.add_join('LEFT JOIN', alias=company_dependent_field_alias, table='ir_property', condition=SQL(
                    """
                        %(ir_property_fields_id)s = %(field_id)s
                        AND %(ir_property_company_id)s = %(root_company_id)s
                        AND %(ir_property_res_id)s = 'account.account,' || %(account_id)s::text
                    """,
                    ir_property_fields_id=self.env['ir.property']._field_to_sql(company_dependent_field_alias, 'fields_id'),
                    field_id=self.env['ir.model.fields']._get(self._name, field.name).id,
                    ir_property_company_id=self.env['ir.property']._field_to_sql(company_dependent_field_alias, 'company_id'),
                    root_company_id=self.env.company.root_id.id,
                    ir_property_res_id=self.env['ir.property']._field_to_sql(company_dependent_field_alias, 'res_id'),
                    account_id=SQL.identifier(alias, 'id'),
                )
            )

            # This is the field on ir.property that will contain the value of 'code'.
            value_field = self.env['ir.property']._fields[TYPE2FIELD[field.type]]

            return self.env['ir.property']._field_to_sql(company_dependent_field_alias, value_field.name)

        return super()._field_to_sql(alias, fname, query, flush)

    @api.constrains('reconcile', 'account_type', 'tax_ids')
    def _constrains_reconcile(self):
        for record in self:
            if record.account_type == 'off_balance':
                if record.reconcile:
                    raise UserError(_('An Off-Balance account can not be reconcilable'))
                if record.tax_ids:
                    raise UserError(_('An Off-Balance account can not have taxes'))

    @api.constrains('allowed_journal_ids')
    def _constrains_allowed_journal_ids(self):
        self.env['account.move.line'].flush_model(['account_id', 'journal_id'])
        self.flush_recordset(['allowed_journal_ids'])
        self._cr.execute("""
            SELECT aml.id
            FROM account_move_line aml
            WHERE aml.account_id in %s
            AND EXISTS (SELECT 1 FROM account_account_account_journal_rel WHERE account_account_id = aml.account_id)
            AND NOT EXISTS (SELECT 1 FROM account_account_account_journal_rel WHERE account_account_id = aml.account_id AND account_journal_id = aml.journal_id)
        """, [tuple(self.ids)])
        ids = self._cr.fetchall()
        if ids:
            raise ValidationError(_('Some journal items already exist with this account but in other journals than the allowed ones.'))

    @api.constrains('currency_id')
    def _check_journal_consistency(self):
        ''' Ensure the currency set on the journal is the same as the currency set on the
        linked accounts.
        '''
        if not self:
            return

        self.env['account.account'].flush_model(['currency_id'])
        self.env['account.journal'].flush_model([
            'currency_id',
            'default_account_id',
            'suspense_account_id',
        ])
        self.env['account.payment.method'].flush_model(['payment_type'])
        self.env['account.payment.method.line'].flush_model(['payment_method_id', 'payment_account_id'])

        self._cr.execute('''
            SELECT
                account.id,
                journal.id
            FROM account_journal journal
            JOIN res_company company ON company.id = journal.company_id
            JOIN account_account account ON account.id = journal.default_account_id
            WHERE journal.currency_id IS NOT NULL
            AND journal.currency_id != company.currency_id
            AND account.currency_id != journal.currency_id
            AND account.id IN %(accounts)s

            UNION ALL

            SELECT
                account.id,
                journal.id
            FROM account_journal journal
            JOIN res_company company ON company.id = journal.company_id
            JOIN account_payment_method_line apml ON apml.journal_id = journal.id
            JOIN account_payment_method apm on apm.id = apml.payment_method_id
            JOIN account_account account ON account.id = COALESCE(apml.payment_account_id, company.account_journal_payment_debit_account_id)
            WHERE journal.currency_id IS NOT NULL
            AND journal.currency_id != company.currency_id
            AND account.currency_id != journal.currency_id
            AND apm.payment_type = 'inbound'
            AND account.id IN %(accounts)s

            UNION ALL

            SELECT
                account.id,
                journal.id
            FROM account_journal journal
            JOIN res_company company ON company.id = journal.company_id
            JOIN account_payment_method_line apml ON apml.journal_id = journal.id
            JOIN account_payment_method apm on apm.id = apml.payment_method_id
            JOIN account_account account ON account.id = COALESCE(apml.payment_account_id, company.account_journal_payment_credit_account_id)
            WHERE journal.currency_id IS NOT NULL
            AND journal.currency_id != company.currency_id
            AND account.currency_id != journal.currency_id
            AND apm.payment_type = 'outbound'
            AND account.id IN %(accounts)s
        ''', {
            'accounts': tuple(self.ids)
        })
        res = self._cr.fetchone()
        if res:
            account = self.env['account.account'].browse(res[0])
            journal = self.env['account.journal'].browse(res[1])
            raise ValidationError(_(
                "The foreign currency set on the journal '%(journal)s' and the account '%(account)s' must be the same.",
                journal=journal.display_name,
                account=account.display_name
            ))

    @api.constrains('company_ids')
    def _check_company_consistency(self):
        if accounts_without_company := self.filtered(lambda a: not a.sudo().company_ids):
            raise ValidationError(
                _("The following accounts must be assigned to at least one company:")
                + "\n" + "\n".join(f"- {account.display_name}" for account in accounts_without_company)
            )
        for companies, accounts in self.grouped(lambda a: a.company_ids).items():
            if self.env['account.move.line'].sudo().search_count([
                ('account_id', 'in', accounts.ids),
                '!', ('company_id', 'child_of', companies.ids)
            ], limit=1):
                raise UserError(_("You can't unlink this company from this account since there are some journal items linked to it."))

    @api.constrains('account_type')
    def _check_account_type_sales_purchase_journal(self):
        if not self:
            return

        self.env['account.account'].flush_model(['account_type'])
        self.env['account.journal'].flush_model(['type', 'default_account_id'])
        self._cr.execute('''
            SELECT account.id
            FROM account_account account
            JOIN account_journal journal ON journal.default_account_id = account.id
            WHERE account.id IN %s
            AND account.account_type IN ('asset_receivable', 'liability_payable')
            AND journal.type IN ('sale', 'purchase')
            LIMIT 1;
        ''', [tuple(self.ids)])

        if self._cr.fetchone():
            raise ValidationError(_("The account is already in use in a 'sale' or 'purchase' journal. This means that the account's type couldn't be 'receivable' or 'payable'."))

    @api.constrains('reconcile')
    def _check_used_as_journal_default_debit_credit_account(self):
        accounts = self.filtered(lambda a: not a.reconcile)
        if not accounts:
            return

        self.env['account.journal'].flush_model(['company_id', 'default_account_id'])
        self.env['res.company'].flush_model(['account_journal_payment_credit_account_id', 'account_journal_payment_debit_account_id'])
        self.env['account.payment.method.line'].flush_model(['journal_id', 'payment_account_id'])

        self._cr.execute('''
            SELECT journal.id
            FROM account_journal journal
            JOIN res_company company on journal.company_id = company.id
            LEFT JOIN account_payment_method_line apml ON journal.id = apml.journal_id
            WHERE (
                company.account_journal_payment_credit_account_id IN %(accounts)s
                AND company.account_journal_payment_credit_account_id != journal.default_account_id
                ) OR (
                company.account_journal_payment_debit_account_id in %(accounts)s
                AND company.account_journal_payment_debit_account_id != journal.default_account_id
                ) OR (
                apml.payment_account_id IN %(accounts)s
                AND apml.payment_account_id != journal.default_account_id
            )
        ''', {
            'accounts': tuple(accounts.ids),
        })

        rows = self._cr.fetchall()
        if rows:
            journals = self.env['account.journal'].browse([r[0] for r in rows])
            raise ValidationError(_(
                "This account is configured in %(journal_names)s journal(s) (ids %(journal_ids)s) as payment debit or credit account. This means that this account's type should be reconcilable.",
                journal_names=journals.mapped('display_name'),
                journal_ids=journals.ids
            ))

    @api.constrains('code')
    def _check_account_code(self):
        for account in self:
            if account.code and not re.match(ACCOUNT_CODE_REGEX, account.code):
                raise ValidationError(_(
                    "The account code can only contain alphanumeric characters and dots."
                ))

    @api.constrains('account_type')
    def _check_account_is_bank_journal_bank_account(self):
        self.env['account.account'].flush_model(['account_type'])
        self.env['account.journal'].flush_model(['type', 'default_account_id'])
        self._cr.execute('''
            SELECT journal.id
              FROM account_journal journal
              JOIN account_account account ON journal.default_account_id = account.id
             WHERE account.account_type IN ('asset_receivable', 'liability_payable')
               AND account.id IN %s
             LIMIT 1;
        ''', [tuple(self.ids)])

        if self._cr.fetchone():
            raise ValidationError(_("You cannot change the type of an account set as Bank Account on a journal to Receivable or Payable."))

    @api.depends_context('company')
    def _compute_code(self):
        values = self.env['ir.property'].with_company(self.env.company.root_id).sudo()._get_multi('code', 'account.account', self.ids)
        for record in self:
            # Need to set record.code with `company = self.env.company`, not `self.env.company.root_id`
            record.code = values.get(record.id)

    def _search_code(self, operator, value):
        return self._fields['code']._search_company_dependent(self.with_company(self.env.company.root_id), operator, value)

    def _inverse_code(self):
        values = {
            # Need to access record.code with `company = self.env.company`
            record.id: self._fields['code'].convert_to_write(record.code, record)
            for record in self
        }
        self.env['ir.property'].with_company(self.env.company.root_id).sudo()._set_multi('code', 'account.account', values)

    @api.depends_context('company')
    @api.depends('code')
    def _compute_account_root(self):
        for record in self:
            record.root_id = self.env['account.root']._from_account_code(record.code)

    def _search_account_root(self, operator, value):
        if operator in ['=', 'child_of']:
            root = self.env['account.root'].browse(value)
            return [('code', '=like', root.name + ('' if operator == '=' and not root.parent_id else '%'))]
        raise NotImplementedError

    def _search_panel_domain_image(self, field_name, domain, set_count=False, limit=False):
        if field_name != 'root_id' or set_count:
            return super()._search_panel_domain_image(field_name, domain, set_count, limit)

        if expression.is_false(self, domain):
            return {}

        query_account = self.env['account.account']._search(domain, limit=limit)
        account_code_alias = self.env['account.account']._field_to_sql('account_account', 'code', query_account)

        account_codes = self.env.execute_query(query_account.select(account_code_alias))
        return {
            (root := self.env['account.root']._from_account_code(code)).id: {'id': root.id, 'display_name': root.display_name}
            for code, in account_codes if code
        }

    @api.depends_context('company')
    @api.depends('code')
    def _compute_account_group(self):
        accounts_with_code = self.filtered(lambda a: a.code)

        (self - accounts_with_code).group_id = False

        if not accounts_with_code:
            return

        codes = accounts_with_code.mapped('code')
        account_code_values = SQL(','.join(['(%s)'] * len(codes)), *codes)
        results = self.env.execute_query(SQL(
            """
                 SELECT DISTINCT ON (account_code.code)
                        account_code.code,
                        agroup.id AS group_id
                   FROM (VALUES %(account_code_values)s) AS account_code (code)
              LEFT JOIN account_group agroup
                     ON agroup.code_prefix_start <= LEFT(account_code.code, char_length(agroup.code_prefix_start))
                        AND agroup.code_prefix_end >= LEFT(account_code.code, char_length(agroup.code_prefix_end))
                        AND agroup.company_id = %(root_company_id)s
               ORDER BY account_code.code, char_length(agroup.code_prefix_start) DESC, agroup.id
            """,
            account_code_values=account_code_values,
            root_company_id=self.env.company.root_id.id,
        ))
        group_by_code = dict(results)

        for account in accounts_with_code:
            account.group_id = group_by_code[account.code]

    def _search_used(self, operator, value):
        if operator not in ['=', '!='] or not isinstance(value, bool):
            raise UserError(_('Operation not supported'))
        if operator != '=':
            value = not value
        self._cr.execute("""
            SELECT id FROM account_account account
            WHERE EXISTS (SELECT 1 FROM account_move_line aml WHERE aml.account_id = account.id LIMIT 1)
        """)
        return [('id', 'in' if value else 'not in', [r[0] for r in self._cr.fetchall()])]

    def _compute_used(self):
        ids = set(self._search_used('=', True)[0][2])
        for record in self:
            record.used = record.id in ids

    @api.model
    def _search_new_account_code(self, start_code, cache=None):
        """ Get an available account code by starting from an existing code
            and incrementing it until an available code is found.

            Examples:
                |  start_code  |  codes checked for availability                            |
                +--------------+------------------------------------------------------------+
                |    102100    |  102101, 102102, 102103, 102104, ...                       |
                |     1598     |  1599, 1600, 1601, 1602, ...                               |
                |   10.01.08   |  10.01.09, 10.01.10, 10.01.11, 10.01.12, ...               |
                |   10.01.97   |  10.01.98, 10.01.99, 10.01.97.copy2, 10.01.97.copy3, ...   |
                |    1021A     |  1021A, 1022A, 1023A, 1024A, ...                           |
                |    hello     |  hello.copy, hello.copy2, hello.copy3, hello.copy4, ...    |
                |     9998     |  9999, 9998.copy, 9998.copy2, 9998.copy3, ...              |

            :param start_code str: the code to increment until an available one is found
            :param set[str] cache: a set of codes which you know are already used
                                    (optional, to speed up the method).
                                    If none is given, the method will use cache = {start_code}.
                                    i.e. the method will return the first available code
                                    *strictly* greater than start_code.
                                    If you want the method to start at start_code, you should
                                    explicitly pass cache={}.

            :return str: an available new account code for `company`.
                         It will normally have length `len(start_code)`.
                         If incrementing the last digits starting from `start_code` does
                         not work, the method will try as a fallback
                         '{start_code}.copy', '{start_code}.copy2', ... '{start_code}.copy99'.
        """
        if cache is None:
            cache = {start_code}

        def code_is_available(new_code):
            return new_code not in cache and not self.search_count([('code', '=', new_code)], limit=1)

        if code_is_available(start_code):
            return start_code

        start_str, digits_str, end_str = ACCOUNT_CODE_NUMBER_REGEX.match(start_code).groups()

        if digits_str != '':
            d, n = len(digits_str), int(digits_str)
            for num in range(n+1, 10**d):
                if code_is_available(new_code := f'{start_str}{num:0{d}}{end_str}'):
                    return new_code

        for num in range(99):
            if code_is_available(new_code := f'{start_code}.copy{num and num + 1 or ""}'):
                return new_code

        raise UserError(_('Cannot generate an unused account code.'))

    @api.depends_context('company')
    def _compute_current_balance(self):
        balances = {
            account.id: balance
            for account, balance in self.env['account.move.line']._read_group(
                domain=[('account_id', 'in', self.ids), ('parent_state', '=', 'posted'), ('company_id', '=', self.env.company.id)],
                groupby=['account_id'],
                aggregates=['balance:sum'],
            )
        }
        for record in self:
            record.current_balance = balances.get(record.id, 0)

    @api.depends_context('company')
    def _compute_related_taxes_amount(self):
        for record in self:
            record.related_taxes_amount = self.env['account.tax'].search_count([
                *self.env['account.tax']._check_company_domain(self.env.company),
                ('repartition_line_ids.account_id', '=', record.id),
            ])

    @api.depends_context('company')
    def _compute_company_currency_id(self):
        self.company_currency_id = self.env.company.currency_id

    @api.depends_context('company')
    def _compute_company_fiscal_country_code(self):
        self.company_fiscal_country_code = self.env.company.account_fiscal_country_id.code

    @api.depends_context('company')
    def _compute_opening_debit_credit(self):
        self.opening_debit = 0
        self.opening_credit = 0
        self.opening_balance = 0
        opening_move = self.env.company.account_opening_move_id
        if not self.ids or not opening_move:
            return
        self.env.cr.execute(SQL(
            """
            SELECT line.account_id,
                   SUM(line.balance) AS balance,
                   SUM(line.debit) AS debit,
                   SUM(line.credit) AS credit
              FROM account_move_line line
             WHERE line.move_id = %(opening_move_id)s
               AND line.account_id IN %(account_ids)s
             GROUP BY line.account_id
            """,
            account_ids=tuple(self.ids),
            opening_move_id=opening_move.id,
        ))
        result = {r['account_id']: r for r in self.env.cr.dictfetchall()}
        for record in self:
            res = result.get(record.id) or {'debit': 0, 'credit': 0, 'balance': 0}
            record.opening_debit = res['debit']
            record.opening_credit = res['credit']
            record.opening_balance = res['balance']

    @api.depends('code')
    def _compute_account_type(self):
        accounts_to_process = self.filtered(lambda account: account.code and not account.account_type)
        self._get_closest_parent_account(accounts_to_process, 'account_type', default_value='asset_current')

    @api.depends('code')
    def _compute_account_tags(self):
        accounts_to_process = self.filtered(lambda account: account.code and not account.tag_ids)
        self._get_closest_parent_account(accounts_to_process, 'tag_ids', default_value=[])

    def _get_closest_parent_account(self, accounts_to_process, field_name, default_value):
        """
            This helper function retrieves the closest parent account based on account codes
            for the given accounts to process and assigns the value of the parent to the specified field.

            :param accounts_to_process: Records of accounts to be processed.
            :param field_name: Name of the field to be updated with the closest parent account value.
            :param default_value: Default value to be assigned if no parent account is found.
        """
        assert field_name in self._fields

        all_accounts = self.search_read(
            domain=self._check_company_domain(self.env.company),
            fields=['code', field_name],
            order='code',
        )
        accounts_with_codes = {}
        # We want to group accounts by company to only search for account codes of the current company
        for account in all_accounts:
            accounts_with_codes[account['code']] = account[field_name]
        for account in accounts_to_process:
            codes_list = list(accounts_with_codes.keys())
            closest_index = bisect_left(codes_list, account.code) - 1
            account[field_name] = accounts_with_codes[codes_list[closest_index]] if closest_index != -1 else default_value

    @api.depends('account_type')
    def _compute_include_initial_balance(self):
        for account in self:
            account.include_initial_balance = account.internal_group not in ['income', 'expense']

    def _search_include_initial_balance(self, operator, value):
        if operator not in ['=', '!='] or not isinstance(value, bool):
            raise UserError(_('Operation not supported'))
        if operator != '=':
            value = not value
        return [('internal_group', 'not in' if value else 'in', ['income', 'expense'])]

    def _get_internal_group(self, account_type):
        return account_type.split('_', maxsplit=1)[0]

    @api.depends('account_type')
    def _compute_internal_group(self):
        for account in self:
            account.internal_group = account.account_type and account._get_internal_group(account.account_type)

    def _search_internal_group(self, operator, value):
        if operator not in ['=', 'in', '!=', 'not in']:
            raise UserError(_('Operation not supported'))
        domain = expression.OR([[('account_type', '=like', group)] for group in {
            self._get_internal_group(v) + '%'
            for v in (value if isinstance(value, (list, tuple)) else [value])
        }])
        if operator in ('!=', 'not in'):
            return ['!'] + expression.normalize_domain(domain)
        return domain

    @api.depends('account_type')
    def _compute_reconcile(self):
        for account in self:
            account.reconcile = account.account_type in ('asset_receivable', 'liability_payable')

    def _set_opening_debit(self):
        for record in self:
            record._set_opening_debit_credit(record.opening_debit, 'debit')

    def _set_opening_credit(self):
        for record in self:
            record._set_opening_debit_credit(record.opening_credit, 'credit')

    def _set_opening_balance(self):
        # Tracking of the balances to be used after the import to populate the opening move in batch.
        for account in self:
            balance = account.opening_balance
            account._set_opening_debit_credit(abs(balance) if balance > 0.0 else 0.0, 'debit')
            account._set_opening_debit_credit(abs(balance) if balance < 0.0 else 0.0, 'credit')

    def _set_opening_debit_credit(self, amount, field):
        """ Generic function called by both opening_debit and opening_credit's
        inverse function. 'Amount' parameter is the value to be set, and field
        either 'debit' or 'credit', depending on which one of these two fields
        got assigned.
        """
        self.ensure_one()
        if 'import_account_opening_balance' not in self._cr.precommit.data:
            data = self._cr.precommit.data['import_account_opening_balance'] = {}
            self._cr.precommit.add(self._load_precommit_update_opening_move)
        else:
            data = self._cr.precommit.data['import_account_opening_balance']
        data.setdefault(self.env.company.id, {}).setdefault(self.id, [None, None])
        index = 0 if field == 'debit' else 1
        data[self.env.company.id][self.id][index] = amount

    @api.model
    def default_get(self, default_fields):
        """If we're creating a new account through a many2one, there are chances that we typed the account code
        instead of its name. In that case, switch both fields values.
        """
        if 'name' not in default_fields and 'code' not in default_fields:
            return super().default_get(default_fields)
        default_name = self._context.get('default_name')
        default_code = self._context.get('default_code')
        if default_name and not default_code:
            try:
                default_code = int(default_name)
            except ValueError:
                pass
            if default_code:
                default_name = False
        contextual_self = self.with_context(default_name=default_name, default_code=default_code)
        return super(AccountAccount, contextual_self).default_get(default_fields)

    @api.model
    def _get_most_frequent_accounts_for_partner(self, company_id, partner_id, move_type, filter_never_user_accounts=False, limit=None):
        """
        Returns the accounts ordered from most frequent to least frequent for a given partner
        and filtered according to the move type
        :param company_id: the company id
        :param partner_id: the partner id for which we want to retrieve the most frequent accounts
        :param move_type: the type of the move to know which type of accounts to retrieve
        :param filter_never_user_accounts: True if we should filter out accounts never used for the partner
        :param limit: the maximum number of accounts to retrieve
        :returns: List of account ids, ordered by frequency (from most to least frequent)
        """
        domain = [
            *self.env['account.move.line']._check_company_domain(company_id),
            ('partner_id', '=', partner_id),
            ('account_id.deprecated', '=', False),
            ('date', '>=', fields.Date.add(fields.Date.today(), days=-365 * 2)),
        ]
        if move_type in self.env['account.move'].get_inbound_types(include_receipts=True):
            domain.append(('account_id.internal_group', '=', 'income'))
        elif move_type in self.env['account.move'].get_outbound_types(include_receipts=True):
            domain.append(('account_id.internal_group', '=', 'expense'))

        query = self.env['account.move.line']._where_calc(domain)
        if not filter_never_user_accounts:
            _kind, rhs_table, condition = query._joins['account_move_line__account_id']
            query._joins['account_move_line__account_id'] = (SQL("RIGHT JOIN"), rhs_table, condition)

        company = self.env['res.company'].browse(company_id)
        code_sql = self.with_company(company)._field_to_sql('account_move_line__account_id', 'code', query)

        return [r[0] for r in self.env.execute_query(SQL(
            """
                SELECT account_move_line__account_id.id
                  FROM %(from_clause)s
                 WHERE %(where_clause)s
              GROUP BY account_move_line__account_id.id
              ORDER BY COUNT(account_move_line.id) DESC, MAX(%(code_sql)s)
                %(limit_clause)s
            """,
            from_clause=query.from_clause,
            where_clause=query.where_clause or SQL("TRUE"),
            code_sql=code_sql,
            limit_clause=SQL("LIMIT %s", limit) if limit else SQL(),
        ))]

    @api.model
    def _get_most_frequent_account_for_partner(self, company_id, partner_id, move_type=None):
        most_frequent_account = self._get_most_frequent_accounts_for_partner(company_id, partner_id, move_type, filter_never_user_accounts=True, limit=1)
        return most_frequent_account[0] if most_frequent_account else False

    @api.model
    def _order_accounts_by_frequency_for_partner(self, company_id, partner_id, move_type=None):
        return self._get_most_frequent_accounts_for_partner(company_id, partner_id, move_type)

    @api.model
    def _name_search(self, name, domain=None, operator='ilike', limit=None, order=None):
        if not name and self._context.get('partner_id') and self._context.get('move_type'):
            return self._order_accounts_by_frequency_for_partner(
                            self.env.company.id, self._context.get('partner_id'), self._context.get('move_type'))
        domain = domain or []
        if name:
            if operator in ('=', '!='):
                name_domain = ['|', ('code', '=', name.split(' ')[0]), ('name', operator, name)]
            else:
                name_domain = ['|', ('code', '=like', name.split(' ')[0] + '%'), ('name', operator, name)]
            if operator in expression.NEGATIVE_TERM_OPERATORS:
                name_domain = ['&', '!'] + name_domain[1:]
            domain = expression.AND([name_domain, domain])
        return self._search(domain, limit=limit, order=order)

    @api.onchange('account_type')
    def _onchange_account_type(self):
        if self.account_type == 'off_balance':
            self.tax_ids = False

    def _split_code_name(self, code_name):
        # We only want to split the name on the first word if there is a digit in it
        code, name = ACCOUNT_REGEX.match(code_name or '').groups()
        return code, name.strip()

    @api.onchange('name')
    def _onchange_name(self):
        code, name = self._split_code_name(self.name)
        if code and not self.code:
            self.name = name
            self.code = code

    @api.depends_context('company')
    @api.depends('code')
    def _compute_display_name(self):
        for account in self:
            account.display_name = f"{account.code} {account.name}"

    def copy_data(self, default=None):
        vals_list = super().copy_data(default)
        default = default or {}

        # We must restrict check_company fields to values available to the company of the new account.
        fields_to_filter_by_company = {field for field in self._fields.values() if field.relational and field.check_company}
        cache = defaultdict(set)
        for account, vals in zip(self, vals_list):
            match vals.get('company_ids'):
                case [(Command.LINK, company_id, 0)] | [(Command.SET, 0, [company_id])] | [company_id] if isinstance(company_id, int):
                    company = self.env['res.company'].browse(company_id)
                case _:
                    raise ValueError(_("You may only give an account a single company at creation."))
            if 'code' not in default:
                start_code = account.with_company(company).code or account.with_company(account.company_ids[0]).code
                vals['code'] = account.with_company(company)._search_new_account_code(start_code, cache[company])
                cache[company].add(vals['code'])
            if 'name' not in default:
                vals['name'] = _("%s (copy)", account.name or '')

            # For check_company fields, only keep values that are compatible with the new account's company.
            for field in fields_to_filter_by_company:
                if field.name not in default:
                    corecord = account[field.name]
                    filtered_corecord = corecord.filtered_domain(corecord._check_company_domain(company))
                    vals[field.name] = filtered_corecord.id if field.type == 'many2one' else [Command.set(filtered_corecord.ids)]
        return vals_list

    def copy_translations(self, new, excluded=()):
        super().copy_translations(new, excluded=tuple(excluded)+('name',))
        if new.name == _('%s (copy)', self.name):
            name_field = self._fields['name']
            self.env.cache.update_raw(new, name_field, [{
                lang: _('%s (copy)', tr)
                for lang, tr in name_field._get_stored_translations(self).items()
            }], dirty=True)

    @api.model
    def _load_precommit_update_opening_move(self):
        """ precommit callback to recompute the opening move according the opening balances that changed.
        This is particularly useful when importing a csv containing the 'opening_balance' column.
        In that case, we don't want to use the inverse method set on field since it will be
        called for each account separately. That would be quite costly in terms of performances.
        Instead, the opening balances are collected and this method is called once at the end
        to update the opening move accordingly.
        """
        data = self._cr.precommit.data.pop('import_account_opening_balance', {})

        for company_id, account_values in data.items():
            self.env['res.company'].browse(company_id)._update_opening_move({
                self.env['account.account'].browse(account_id): values
                for account_id, values in account_values.items()
            })

    def _toggle_reconcile_to_true(self):
        '''Toggle the `reconcile´ boolean from False -> True

        Note that: lines with debit = credit = amount_currency = 0 are set to `reconciled´ = True
        '''
        if not self.ids:
            return None
        query = """
            UPDATE account_move_line SET
                reconciled = CASE WHEN debit = 0 AND credit = 0 AND amount_currency = 0
                    THEN true ELSE false END,
                amount_residual = (debit-credit),
                amount_residual_currency = amount_currency
            WHERE full_reconcile_id IS NULL and account_id IN %s
        """
        self.env.cr.execute(query, [tuple(self.ids)])
        self.env['account.move.line'].invalidate_model(['amount_residual', 'amount_residual_currency', 'reconciled'])

    def _toggle_reconcile_to_false(self):
        '''Toggle the `reconcile´ boolean from True -> False

        Note that it is disallowed if some lines are partially reconciled.
        '''
        if not self.ids:
            return None
        partial_lines_count = self.env['account.move.line'].search_count([
            ('account_id', 'in', self.ids),
            ('full_reconcile_id', '=', False),
            ('|'),
            ('matched_debit_ids', '!=', False),
            ('matched_credit_ids', '!=', False),
        ])
        if partial_lines_count > 0:
            raise UserError(_('You cannot switch an account to prevent the reconciliation '
                              'if some partial reconciliations are still pending.'))
        query = """
            UPDATE account_move_line
                SET amount_residual = 0, amount_residual_currency = 0
            WHERE full_reconcile_id IS NULL AND account_id IN %s
        """
        self.env.cr.execute(query, [tuple(self.ids)])

    @api.model
    def name_create(self, name):
        """ Split the account name into account code and account name in import.
        When importing a file with accounts, the account code and name may be both entered in the name column.
        In this case, the name will be split into code and name.
        """
        if 'import_file' in self.env.context:
            code, name = self._split_code_name(name)
            record = self.create({'code': code, 'name': name})
            return record.id, record.display_name
        raise ValidationError(_("Please create new accounts from the Chart of Accounts menu."))

    @api.model_create_multi
    def create(self, vals_list):
        records_list = []
        for company_ids, vals_list_for_company in itertools.groupby(vals_list, lambda v: v.get('company_ids')):
            match company_ids:
                case None:
                    company = self.env.company
                case [(Command.LINK, company_id, *_)] | [(Command.SET, 0, [company_id])] | [company_id] if isinstance(company_id, int):
                    company = self.env['res.company'].browse(company_id)
                case _:
                    raise ValueError(_("You may only give an account a single company at creation."))
            cache = set()
            vals_list_for_company = list(vals_list_for_company)
            for vals in vals_list_for_company:
                if 'prefix' in vals:
                    prefix, digits = vals.pop('prefix'), vals.pop('code_digits')
                    start_code = prefix.ljust(digits - 1, '0') + '1' if len(prefix) < digits else prefix
                    vals['code'] = self.with_company(company)._search_new_account_code(start_code, cache)
                    cache.add(vals['code'])
            records_list.append(super(AccountAccount, self.with_company(company)).create(vals_list_for_company))
        records = self.env['account.account'].union(*records_list)
        records.with_context(allowed_company_ids=records.company_ids.ids)._ensure_code_is_unique()
        return records

    def write(self, vals):
        if 'reconcile' in vals:
            if vals['reconcile']:
                self.filtered(lambda r: not r.reconcile)._toggle_reconcile_to_true()
            else:
                self.filtered(lambda r: r.reconcile)._toggle_reconcile_to_false()

        if vals.get('currency_id'):
            for account in self:
                if self.env['account.move.line'].search_count([('account_id', '=', account.id), ('currency_id', 'not in', (False, vals['currency_id']))]):
                    raise UserError(_('You cannot set a currency on this account as it already has some journal entries having a different foreign currency.'))
        res = super().write(vals)
        if {'company_ids', 'code'} & vals.keys():
            self._ensure_code_is_unique()
        return res

    def _ensure_code_is_unique(self):
        """ Ensure that for each company to which the account belongs, the code is set
        and that codes are unique per-company. """
        accounts = self.sudo()
        for account in accounts:
            for company in account.company_ids:
                if not account.with_company(company).code:
                    raise ValidationError(_("The code must be set for every company to which this account belongs."))
        accounts_with_code = accounts.filtered(lambda a: a.code)
        accounts_by_code = accounts_with_code.grouped('code')
        duplicate_codes = None
        if len(accounts_by_code) < len(accounts_with_code):
            duplicate_codes = [code for code, accounts in accounts_by_code.items() if len(accounts) > 1]
        # search for duplicates of self in database
        elif duplicates := self.sudo().search_fetch(
            [
                ('code', 'in', list(accounts_by_code)),
                ('id', 'not in', self.ids),
            ],
            ['code'],
        ):
            duplicate_codes = duplicates.mapped('code')
        if duplicate_codes:
            raise ValidationError(
                _("Account codes must be unique. You can't create accounts with these duplicate codes: %s", ", ".join(duplicate_codes))
            )

    def _load_records_write(self, values):
        if 'prefix' in values:
            del values['code_digits']
            del values['prefix']
        super()._load_records_write(values)

    @api.ondelete(at_uninstall=False)
    def _unlink_except_contains_journal_items(self):
        if self.env['account.move.line'].search_count([('account_id', 'in', self.ids)], limit=1):
            raise UserError(_('You cannot perform this action on an account that contains journal items.'))

    @api.ondelete(at_uninstall=False)
    def _unlink_except_account_set_on_customer(self):
        #Checking whether the account is set as a property to any Partner or not
        values = ['account.account,%s' % (account_id,) for account_id in self.ids]
        partner_prop_acc = self.env['ir.property'].sudo().search([('value_reference', 'in', values)], limit=1)
        if partner_prop_acc:
            account_name = partner_prop_acc.get_by_record().display_name
            raise UserError(
                _("You can't delete the account %s, as it is used on a contact.\n\n"
                    "Think of it as safeguarding your customer's receivables; your CFO would appreciate it :)"
                    , account_name)
            )

    @api.ondelete(at_uninstall=False)
    def _unlink_except_linked_to_fiscal_position(self):
        if self.env['account.fiscal.position.account'].search_count(['|', ('account_src_id', 'in', self.ids), ('account_dest_id', 'in', self.ids)], limit=1):
            raise UserError(_('You cannot remove/deactivate the accounts "%s" which are set on the account mapping of a fiscal position.', ', '.join(f"{a.code} - {a.name}" for a in self)))

    @api.ondelete(at_uninstall=False)
    def _unlink_except_linked_to_tax_repartition_line(self):
        if self.env['account.tax.repartition.line'].search_count([('account_id', 'in', self.ids)], limit=1):
            raise UserError(_('You cannot remove/deactivate the accounts "%s" which are set on a tax repartition line.', ', '.join(f"{a.code} - {a.name}" for a in self)))

    def action_open_related_taxes(self):
        related_taxes_ids = self.env['account.tax'].search([
            ('repartition_line_ids.account_id', '=', self.id),
        ]).ids
        return {
            'type': 'ir.actions.act_window',
            'name': _('Taxes'),
            'res_model': 'account.tax',
            'views': [[False, 'list'], [False, 'form']],
            'domain': [('id', 'in', related_taxes_ids)],
        }

    @api.model
    def get_import_templates(self):
        return [{
            'label': _('Import Template for Chart of Accounts'),
            'template': '/account/static/xls/coa_import_template.xlsx'
        }]

    def _merge_method(self, destination, source):
        raise UserError(_("You cannot merge accounts."))

    def action_merge(self):
        """ Merge the accounts in `self`:
            - the first one is extended to each company of the accounts in `self`, keeping their codes and names;
            - the others are deleted; and
            - journal items and other references are retargeted to the first account.

            If the companies of several accounts overlap, this is an accounting operation, so we check that no impacted entries are in a locked period.
            If the companies don't overlap, from an accounting perspective, each company still has its own independent view of the account.
        """
        # Step 1: Perform checks and get account to merge into.
        account_to_merge_into, code_by_company = self._check_action_merge_possible()

        # Step 2: If needed, ask the user for confirmation.
        self._action_merge_get_user_confirmation(account_to_merge_into)

        # Step 3: Perform merge.
        self._action_merge(account_to_merge_into, code_by_company)

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'type': 'success',
                'sticky': False,
                'message': _("Accounts successfully merged!"),
                'next': {'type': 'ir.actions.act_window_close'},
            }
        }

    def _check_action_merge_possible(self):
        """ Perform checks to determine whether the accounts in `self` can be merged,
        and return an account to merge the others into.

        :return: (account_to_merge_into, code_by_company)
        where account_to_merge_into is the account that other accounts should be merged into,
        and code_by_company is a dict of the codes that should be given to the merged account.
        """
        if len(self) < 2:
            raise UserError(_("You must select at least 2 accounts to merge."))

        for field in ['currency_id', 'deprecated', 'account_type', 'reconcile', 'non_trade']:
            if len(set(self.mapped(field))) > 1:
                raise UserError(_(
                    "You may only merge accounts that have the same account type, currency, deprecated status, "
                    "reconciliation status, and trade/non-trade receivable status."
                ))

        # If there are hashed entries in an account, then the merge must preserve that account's.
        accounts_with_hashed_entries = self.filtered(
            lambda a: self.env['account.move.line'].sudo().search_count([
                ('account_id', '=', a.id),
                ('parent_state', '=', 'posted'),
                ('move_id.inalterable_hash', '!=', False)
            ], limit=1)
        )

        match len(accounts_with_hashed_entries):
            case 0:
                account_to_merge_into = None
                code_by_company = {}
            case 1:
                # If exactly one of the accounts contains hashed entries, we must merge the other ones into it,
                # otherwise the hash will be broken.
                account_to_merge_into = accounts_with_hashed_entries
                code_by_company = {company: account_to_merge_into.with_company(company).sudo().code for company in account_to_merge_into.company_ids}
            case _:
                raise UserError(_(
                    "Accounts %s contain hashed entries, so cannot be merged.",
                    ", ".join(accounts_with_hashed_entries.mapped('display_name'))
                ))

        # If there are locked entries in an account, then the merge must preserve that account's code in the companies of those locked entries.
        accounts_by_company = defaultdict(lambda: self.env['account.account'])
        for account in self:
            for company in account.company_ids:
                accounts_by_company[company] |= account

        for company, accounts in accounts_by_company.items():
            if len(accounts) > 1 and (user_lock_date := max(company.user_fiscalyear_lock_date, company.user_hard_lock_date)):
                locked_accounts = accounts.filtered(
                    lambda a: self.env['account.move.line'].sudo().search_count([
                        ('account_id', '=', a.id),
                        ('company_id', '=', company.id),
                        ('parent_state', '=', 'posted'),
                        ('date', '<=', user_lock_date),
                    ], limit=1)
                )
                if not locked_accounts:
                    pass
                elif company in code_by_company and locked_accounts != account_to_merge_into:
                    raise UserError(_(
                        "Company %(company_name)s (lock date %(lock_date)s): "
                        "cannot merge account %(hashed_account_name)s that contains hashed entries "
                        "with accounts %(locked_account_names)s that contain locked entries.",
                        company_name=company.name,
                        lock_date=user_lock_date,
                        hashed_account_name=account_to_merge_into.display_name,
                        locked_account_names=", ".join(a.display_name for a in locked_accounts - account_to_merge_into),
                    ))
                elif len(locked_accounts) == 1:
                    code_by_company[company] = locked_accounts.with_company(company).sudo().code
                else:
                    raise UserError(_(
                        "Company %(company_name)s (lock date %(lock_date)s): "
                        "cannot merge accounts %(locked_account_names)s that both contain locked entries.",
                        company_name=company.name,
                        lock_date=user_lock_date,
                        locked_account_names=", ".join(account.display_name for account in locked_accounts),
                    ))
            else:
                code_by_company[company] = accounts[0].with_company(company).sudo().code

        return account_to_merge_into or self[0], code_by_company

    def _action_merge_get_user_confirmation(self, account_to_merge_into):
        """ Open a RedirectWarning asking the user whether to proceed with the merge. """
        if self.env.context.get('account_merge_confirm'):
            return

        is_irreversible = len(self.company_ids) != sum(len(account.company_ids) for account in self)
        accounts_to_remove = self - account_to_merge_into

        msg = _("Are you sure? This will perform the following operations:\n")
        for account in accounts_to_remove:
            msg += _(
                "- %(account_1)s (company: %(companies_1)s) will be merged into %(account_2)s (company: %(companies_2)s)\n",
                account_1=account.with_company(account.company_ids[:1]).display_name,
                companies_1=",".join(account.company_ids.mapped('name')),
                account_2=account_to_merge_into.with_company(account_to_merge_into.company_ids[:1]).display_name,
                companies_2=",".join(account_to_merge_into.company_ids.mapped('name')),
            )
        if is_irreversible:
            msg += _(
                "This cannot be undone because you are merging accounts belonging to the same company.\n"
                "After merging, we won't be able to separate journal items based on which account they originally referenced."
            )
        action = self.env['ir.actions.actions']._for_xml_id('account.action_merge_accounts')
        raise RedirectWarning(msg, action, _("Merge"), additional_context={**self.env.context, 'account_merge_confirm': True})

    def _action_merge(self, account_to_merge_into, code_by_company):
        """ Perform the merge of the accounts in `self` into `account_to_merge_into`.
        This will update the account codes of `account_to_merge_into` based on those of `self`,
        and update keys in DB from `self` to `account_to_merge_into`.
        This method expects checks to already have been performed.
        """
        # Step 1: Keep track of the company_ids we should write on the account.
        # We will do so only at the end, to avoid triggering the constraint that prevents duplicate codes.
        # Writing the codes will be handled by the update of ir_property.
        company_ids_to_write = self.sudo().company_ids

        # Step 2: Check that we have write access to all the accounts and access to all the companies
        # of these accounts.
        self.check_access_rights('write')
        self.check_access_rule('write')
        if forbidden_companies := (self.sudo().company_ids - self.env.user.company_ids):
            raise UserError(_(
                "You do not have the right to perform this operation as you do not have access to the following companies: %s.",
                ", ".join(c.name for c in forbidden_companies)
            ))

        # Step 3: Update records in DB.
        accounts_to_remove = self - account_to_merge_into

        # 3.1: Update foreign keys in DB
        wiz = self.env['base.partner.merge.automatic.wizard'].new()
        wiz._update_foreign_keys_generic('account.account', accounts_to_remove, account_to_merge_into)

        # 3.2: Update Reference and Many2OneReference fields that reference account.account
        wiz._update_reference_fields_generic('account.account', accounts_to_remove, account_to_merge_into)

        # Step 4: Remove merged accounts
        self.env.invalidate_all()
        self.env.cr.execute(SQL(
            """
             DELETE FROM account_account
              WHERE id IN %(account_ids_to_delete)s
            """,
            account_ids_to_delete=tuple(accounts_to_remove.ids),
        ))

        # Clear ir.model.data ormcache
        self.env.registry.clear_cache()

        # Step 5: Write company_ids and codes on the account
        for company, code in code_by_company.items():
            account_to_merge_into.with_company(company).sudo().code = code

        account_to_merge_into.sudo().company_ids = company_ids_to_write


class AccountGroup(models.Model):
    _name = "account.group"
    _description = 'Account Group'
    _order = 'code_prefix_start'
    _check_company_auto = True
    _check_company_domain = models.check_company_domain_parent_of

    parent_id = fields.Many2one('account.group', index=True, ondelete='cascade', readonly=True, check_company=True)
    name = fields.Char(required=True, translate=True)
    code_prefix_start = fields.Char(compute='_compute_code_prefix_start', readonly=False, store=True, precompute=True)
    code_prefix_end = fields.Char(compute='_compute_code_prefix_end', readonly=False, store=True, precompute=True)
    company_id = fields.Many2one('res.company', required=True, readonly=True, default=lambda self: self.env.company)

    _sql_constraints = [
        (
            'check_length_prefix',
            'CHECK(char_length(COALESCE(code_prefix_start, \'\')) = char_length(COALESCE(code_prefix_end, \'\')))',
            'The length of the starting and the ending code prefix must be the same'
        ),
    ]

    @api.depends('code_prefix_start')
    def _compute_code_prefix_end(self):
        for group in self:
            if not group.code_prefix_end or (group.code_prefix_start and group.code_prefix_end < group.code_prefix_start):
                group.code_prefix_end = group.code_prefix_start

    @api.depends('code_prefix_end')
    def _compute_code_prefix_start(self):
        for group in self:
            if not group.code_prefix_start or (group.code_prefix_end and group.code_prefix_start > group.code_prefix_end):
                group.code_prefix_start = group.code_prefix_end

    @api.depends('code_prefix_start', 'code_prefix_end')
    def _compute_display_name(self):
        for group in self:
            prefix = group.code_prefix_start and str(group.code_prefix_start)
            if prefix and group.code_prefix_end != group.code_prefix_start:
                prefix += '-' + str(group.code_prefix_end)
            group.display_name = ' '.join(filter(None, [prefix, group.name]))


    @api.model
    def _name_search(self, name, domain=None, operator='ilike', limit=None, order=None):
        domain = domain or []
        if operator != 'ilike' or (name or '').strip():
            criteria_operator = ['|'] if operator not in expression.NEGATIVE_TERM_OPERATORS else ['&', '!']
            name_domain = criteria_operator + [('code_prefix_start', '=ilike', name + '%'), ('name', operator, name)]
            domain = expression.AND([name_domain, domain])
        return self._search(domain, limit=limit, order=order)

    @api.constrains('code_prefix_start', 'code_prefix_end')
    def _constraint_prefix_overlap(self):
        self.flush_model()
        query = """
            SELECT other.id FROM account_group this
            JOIN account_group other
              ON char_length(other.code_prefix_start) = char_length(this.code_prefix_start)
             AND other.id != this.id
             AND other.company_id = this.company_id
             AND (
                other.code_prefix_start <= this.code_prefix_start AND this.code_prefix_start <= other.code_prefix_end
                OR
                other.code_prefix_start >= this.code_prefix_start AND this.code_prefix_end >= other.code_prefix_start
            )
            WHERE this.id IN %(ids)s
        """
        self.env.cr.execute(query, {'ids': tuple(self.ids)})
        res = self.env.cr.fetchall()
        if res:
            raise ValidationError(_('Account Groups with the same granularity can\'t overlap'))

    def _sanitize_vals(self, vals):
        if vals.get('code_prefix_start') and 'code_prefix_end' in vals and not vals['code_prefix_end']:
            del vals['code_prefix_end']
        if vals.get('code_prefix_end') and 'code_prefix_start' in vals and not vals['code_prefix_start']:
            del vals['code_prefix_start']
        return vals

    @api.constrains('parent_id')
    def _check_parent_not_circular(self):
        if self._has_cycle():
            raise ValidationError(_("You cannot create recursive groups."))

    @api.model_create_multi
    def create(self, vals_list):
        groups = super().create([self._sanitize_vals(vals) for vals in vals_list])
        groups._adapt_parent_account_group()
        return groups

    def write(self, vals):
        res = super(AccountGroup, self).write(self._sanitize_vals(vals))
        if 'code_prefix_start' in vals or 'code_prefix_end' in vals:
            self._adapt_parent_account_group()
        return res

    def unlink(self):
        for record in self:
            children_ids = self.env['account.group'].search([('parent_id', '=', record.id)])
            children_ids.write({'parent_id': record.parent_id.id})
        return super().unlink()

    def _adapt_parent_account_group(self, company=None):
        """Ensure consistency of the hierarchy of account groups.

        Find and set the most specific parent for each group.
        The most specific is the one with the longest prefixes and with the starting
        prefix being smaller than the child prefixes and the ending prefix being greater.
        """
        if self.env.context.get('delay_account_group_sync'):
            return

        company_ids = company.ids if company else self.company_id.ids
        if not company_ids:
            return

        self.flush_model()
        query = SQL("""
            WITH relation AS (
                SELECT DISTINCT ON (child.id)
                       child.id AS child_id,
                       parent.id AS parent_id
                  FROM account_group parent
                  JOIN account_group child
                    ON char_length(parent.code_prefix_start) < char_length(child.code_prefix_start)
                   AND parent.code_prefix_start <= LEFT(child.code_prefix_start, char_length(parent.code_prefix_start))
                   AND parent.code_prefix_end >= LEFT(child.code_prefix_end, char_length(parent.code_prefix_end))
                   AND parent.id != child.id
                   AND parent.company_id = child.company_id
                 WHERE child.company_id IN %s
                   AND child.parent_id IS DISTINCT FROM parent.id -- IMPORTANT avoid to update if nothing changed
              ORDER BY child.id, char_length(parent.code_prefix_start) DESC
            )
            UPDATE account_group child
               SET parent_id = relation.parent_id
              FROM relation
             WHERE child.id = relation.child_id
         RETURNING child.id
        """, tuple(company_ids))
        self.env.cr.execute(query)

        updated_rows = self.env.cr.fetchall()
        if updated_rows:
            self.invalidate_model(['parent_id'])
