from django.db.models import Q
from django.db.models.constants import LOOKUP_SEP
from django.db.models.expressions import BaseExpression, F
from django.db.models.sql import Query
from django.db.models.sql.constants import QUERY_TERMS
from django.utils.six import wraps

from txmoney.money.models.fields import CurrencyField, MoneyField
from txmoney.money.models.models import Money
from txmoney.money.models.utils import (
    get_currency_field_name, prepare_expression
)

try:
    from django.utils.encoding import smart_unicode
except ImportError:
    from django.utils.encoding import smart_text as smart_unicode


RELEVANT_QUERYSET_METHODS = ('distinct', 'get', 'get_or_create', 'filter', 'exclude')
EXPAND_EXCLUSIONS = {
    'get_or_create': ('defaults', )
}


def _get_clean_name(name):
    # Get rid of __lt, __gt etc for the currency lookup
    path = name.split(LOOKUP_SEP)
    if path[-1] in QUERY_TERMS:
        return LOOKUP_SEP.join(path[:-1])
    else:
        return name


def _get_field(model, name):
    from django.db.models.fields import FieldDoesNotExist

    # Create a fake query object so we can easily work out what field
    # type we are dealing with
    qs = Query(model)
    opts = qs.get_meta()

    parts = name.split(LOOKUP_SEP)

    # The following is borrowed from the innards of Query.add_filter - it strips out __gt, __exact et al.
    num_parts = len(parts)
    if num_parts > 1 and parts[-1] in qs.query_terms:
        # Traverse the lookup query to distinguish related fields from
        # lookup types.
        for counter, field_name in enumerate(parts, 1):
            try:
                lookup_field = model._meta.get_field(field_name)
            except FieldDoesNotExist:
                # Not a field. Bail out.
                parts.pop()
                break
            # Unless we're at the end of the list of lookups, let's attempt
            # to continue traversing relations.
            if counter < num_parts:
                try:
                    model = lookup_field.rel.to
                except AttributeError:
                    # Not a related field. Bail out.
                    parts.pop()
                    break

    return qs.names_to_path(parts, opts, True, fail_on_missing=False)[1]


def is_in_lookup(name, value):
    return hasattr(value, '__iter__') & (name.split(LOOKUP_SEP)[-1] == 'in')


def _convert_in_lookup(model, field_name, options):
    """
    ``in`` lookup can not be represented as keyword lookup.
    It requires transformation to combination of ``Q`` objects.
    Example:
        amount__in=[Money(10, 'EUR'), Money(5, 'USD)]
        is equivalent to:
        Q(amount=10, amount_currency='EUR') or Q(amount=5, amount_currency='USD')
    """
    field = _get_field(model, field_name)
    new_query = Q()
    for value in options:
        new_query |= Q(**{
            field.name: value.amount,
            get_currency_field_name(field.name): value.currency
        })
    return new_query


def _expand_money_args(model, args):
    """
    Augments args so that they contain _currency lookups - ie.. Q() | Q()
    """
    for arg in args:
        if isinstance(arg, Q):
            for i, child in enumerate(arg.children):
                if isinstance(child, Q):
                    _expand_money_args(model, [child])
                elif isinstance(child, (list, tuple)):
                    name, value = child
                    if isinstance(value, Money):
                        clean_name = _get_clean_name(name)
                        arg.children[i] = Q(*[
                            child,
                            (get_currency_field_name(clean_name), smart_unicode(value.currency))
                        ])
                    field = _get_field(model, name)
                    if isinstance(value, (BaseExpression, F)):
                        if isinstance(field, MoneyField):
                            clean_name = _get_clean_name(name)
                            arg.children[i] = Q(*[
                                child,
                                (get_currency_field_name(clean_name), F(get_currency_field_name(value.name)))
                            ])
                    if isinstance(field, MoneyField) and is_in_lookup(name, value):
                        arg.children[i] = _convert_in_lookup(model, name, value)
    return args


def _expand_money_kwargs(model, args=(), kwargs=None, exclusions=()):
    """
    Augments kwargs so that they contain _currency lookups.
    """
    involved_fields = [_get_clean_name(name) for name in kwargs]
    for name, value in list(kwargs.items()):
        if name in exclusions:
            continue
        if isinstance(value, Money):
            clean_name = _get_clean_name(name)
            kwargs[name] = value.amount
            kwargs[get_currency_field_name(clean_name)] = smart_unicode(value.currency)
        else:
            field = _get_field(model, name)
            if isinstance(field, MoneyField):
                if isinstance(value, (BaseExpression, F)):
                    clean_name = _get_clean_name(name)
                    if not isinstance(value, F):
                        value = prepare_expression(value)
                    kwargs[get_currency_field_name(clean_name)] = F(get_currency_field_name(value.name))
                if is_in_lookup(name, value):
                    args += (_convert_in_lookup(model, name, value), )
                    del kwargs[name]
            elif isinstance(field, CurrencyField):
                money_field_name = name[:-9]  # Remove '_currency'
                if money_field_name not in involved_fields:
                    money_field = _get_field(model, money_field_name)
                    kwargs[money_field_name] = money_field.default.amount

    return args, kwargs


def _get_model(args, func):
    """
    Returns the model class for given function.
    Note, that ``self`` is not available for proxy models.
    """
    if hasattr(func, '__self__'):
        # Bound method
        model = func.__self__.model
    elif hasattr(func, '__wrapped__'):
        # Proxy model
        model = func.__wrapped__.__self__.model
    else:
        # Custom method on user-defined model manager.
        model = args[0].model
    return model


def understands_money(func):
    """
    Used to wrap a queryset method with logic to expand
    a query from something like:
    mymodel.objects.filter(money=Money(100, "USD"))
    To something equivalent to:
    mymodel.objects.filter(money=Decimal("100.0"), money_currency="USD")
    """

    @wraps(func)
    def wrapper(*args, **kwargs):
        model = _get_model(args, func)
        args = _expand_money_args(model, args)
        exclusions = EXPAND_EXCLUSIONS.get(func.__name__, ())
        args, kwargs = _expand_money_kwargs(model, args, kwargs, exclusions)
        return func(*args, **kwargs)

    return wrapper


def add_money_comprehension_to_queryset(qs):
    # Decorate each relevant method with understands_money in the queryset given
    for attr in RELEVANT_QUERYSET_METHODS:
        setattr(qs, attr, understands_money(getattr(qs, attr)))
    return qs


def money_manager(manager):
    """
    Patches a model manager's get_queryset method so that each QuerySet it returns
    is able to work on money fields.
    This allow users of django-money to use other managers while still doing
    money queries.
    """

    # Need to dynamically subclass to add our behaviour, and then change
    # the class of 'manager' to our subclass.

    # Rejected alternatives:
    #
    # * A monkey patch that adds things to the manager instance dictionary.
    #   This fails due to complications with Manager._copy_to_model behaviour.
    #
    # * Returning a new MoneyManager instance (rather than modifying
    #   the passed in manager instance). This fails for reasons that
    #   are tricky to get to the bottom of - Manager does funny things.
    class MoneyManager(manager.__class__):

        def get_queryset(self, *args, **kwargs):
            # If we are calling code that is pre-Django 1.6, need to
            # spell it 'get_query_set'
            s = super(MoneyManager, self)
            method = getattr(s, 'get_queryset', getattr(s, 'get_query_set', None))
            return add_money_comprehension_to_queryset(method(*args, **kwargs))

    manager.__class__ = MoneyManager
    return manager