# utils/analytics.py
"""
What a pile of receipts says that no single receipt can.

utils/compliance answers "is this receipt sound?". This module answers the questions
that only appear once there are several: is this supplier charging more than they used
to, is the same thing cheaper somewhere else, is this purchase unusual for us, which
suppliers make us wait for their uploads, and which receipts were collected somewhere
this vendor has never been.

Three rules run through all of it:

  * Nothing is reported from a single observation. A "trend" drawn through one point
    is noise with a percentage attached, and acting on it costs real money.
  * Every finding carries the evidence it was drawn from - how many receipts, over
    what period - so the reader can judge it rather than trust it.
  * These are prompts to look, never determinations. A price rise may be a different
    grade of the same product; an outlier may be a legitimate one-off.

Everything here is a pure function of the receipts it is handed, so it is testable
without a database and cheap enough to compute on request.
"""
from collections import defaultdict
from datetime import date
from decimal import Decimal
from statistics import mean, pstdev

from utils.classify import normalise_description
from utils.geo import distance_km, median_point, parse_location, region_for

# Nothing is reported until there is this much history behind it.
MIN_PRICE_HISTORY = 2
MIN_ANOMALY_HISTORY = 5

# Movements smaller than this are noise: EFD rounding, a promotion, a different pack.
PRICE_CHANGE_THRESHOLD_PCT = Decimal('10')
CHEAPER_ELSEWHERE_THRESHOLD_PCT = Decimal('10')

# How far from a supplier's usual location a receipt has to be collected before it is
# worth a second look. Generous, because a supplier can legitimately have branches.
LOCATION_OUTLIER_KM = 75

# Located receipts a supplier needs before any of them can be called unusual. Each is
# judged against the other three, and three is the smallest number whose median is
# actually a middle value rather than the midpoint of a pair - with two, one distant
# receipt drags the baseline half way towards itself and every receipt looks wrong.
MIN_LOCATION_HISTORY = 4

# Standard deviations above a category's mean before a receipt is called unusual.
ANOMALY_SIGMA = 2.0


def _vendor_key(receipt):
    """One supplier, however their name is spelled on any given receipt."""
    if receipt.vendor_id:
        return f'id:{receipt.vendor_id}'
    if receipt.vendor_tin:
        return f'tin:{receipt.vendor_tin}'
    return f'name:{normalise_description(receipt.vendor_name)}' if receipt.vendor_name else None


def _vendor_name(receipt):
    return receipt.vendor_name or 'Unnamed vendor'


def _spending(receipts):
    """Only receipts that record money actually spent."""
    return [receipt for receipt in receipts if receipt.is_expense]


def _unit_prices(receipts):
    """
    Every (item, vendor) observation with a usable unit price.

    A line only yields one when it carries both a quantity and an amount; plenty of
    Tanzanian EFDs print 'SUMMARIZED SALE' with no quantity at all, and those are
    skipped rather than guessed at.
    """
    observations = defaultdict(list)
    for receipt in _spending(receipts):
        vendor = _vendor_key(receipt)
        if vendor is None or receipt.receipt_date is None:
            continue

        for item in receipt.items:
            quantity = item.quantity
            if not quantity or Decimal(quantity) <= 0 or not item.amount_cents:
                continue

            item_key = normalise_description(item.description)
            if not item_key:
                continue

            observations[(item_key, vendor)].append({
                'date': receipt.receipt_date,
                'unit_cents': int(Decimal(item.amount_cents) / Decimal(quantity)),
                'description': item.description,
                'vendor_name': _vendor_name(receipt),
                'receipt_id': receipt.id,
            })

    for series in observations.values():
        series.sort(key=lambda entry: entry['date'])
    return observations


def unit_price_movements(receipts, threshold_pct=PRICE_CHANGE_THRESHOLD_PCT,
                         min_history=MIN_PRICE_HISTORY, limit=8):
    """
    Items whose latest unit price departs from what this supplier used to charge.

    The comparison is against the mean of every earlier observation rather than
    against the previous one, so a single odd receipt does not become the baseline
    that everything after it is judged by.
    """
    findings = []
    for series in _unit_prices(receipts).values():
        if len(series) < min_history + 1:
            continue

        latest, history = series[-1], series[:-1]
        baseline = int(mean(entry['unit_cents'] for entry in history))
        if baseline <= 0:
            continue

        change = (Decimal(latest['unit_cents'] - baseline) / Decimal(baseline)) * 100
        if abs(change) < threshold_pct:
            continue

        findings.append({
            'description': latest['description'],
            'vendor_name': latest['vendor_name'],
            'receipt_id': latest['receipt_id'],
            'latest_cents': latest['unit_cents'],
            'baseline_cents': baseline,
            'change_pct': float(round(change, 1)),
            'observations': len(history),
            'since': history[0]['date'],
            'on': latest['date'],
        })

    findings.sort(key=lambda finding: abs(finding['change_pct']), reverse=True)
    return findings[:limit]


def cheaper_elsewhere(receipts, threshold_pct=CHEAPER_ELSEWHERE_THRESHOLD_PCT, limit=8):
    """
    The same item, bought more cheaply from somebody else.

    Each supplier is represented by their most recent price for the item, and only
    items bought from more than one supplier can say anything at all. Whether the
    cheaper supplier is actually usable - stock, distance, credit terms - is not on
    the receipt, so this reports the gap and leaves the decision alone.
    """
    by_item = defaultdict(dict)
    for (item_key, vendor), series in _unit_prices(receipts).items():
        by_item[item_key][vendor] = series[-1]

    findings = []
    for vendors in by_item.values():
        if len(vendors) < 2:
            continue

        offers = sorted(vendors.values(), key=lambda entry: entry['unit_cents'])
        cheapest = offers[0]
        # Judged against the supplier bought from most recently, which is the one the
        # next order would go to by default.
        current = max(vendors.values(), key=lambda entry: entry['date'])
        if current['vendor_name'] == cheapest['vendor_name'] or cheapest['unit_cents'] <= 0:
            continue

        saving = (
            Decimal(current['unit_cents'] - cheapest['unit_cents'])
            / Decimal(current['unit_cents'])
        ) * 100
        if saving < threshold_pct:
            continue

        findings.append({
            'description': current['description'],
            'current_vendor': current['vendor_name'],
            'current_cents': current['unit_cents'],
            'cheapest_vendor': cheapest['vendor_name'],
            'cheapest_cents': cheapest['unit_cents'],
            'saving_pct': float(round(saving, 1)),
            'cheapest_receipt_id': cheapest['receipt_id'],
            'cheapest_on': cheapest['date'],
        })

    findings.sort(key=lambda finding: finding['saving_pct'], reverse=True)
    return findings[:limit]


def spend_anomalies(receipts, sigma=ANOMALY_SIGMA, min_history=MIN_ANOMALY_HISTORY, limit=8):
    """
    Receipts far above what this category normally costs.

    Measured per category, because 'unusually large' means nothing across a ledger
    holding both airtime and rent. The population standard deviation is used rather
    than the sample one: this is the whole set of receipts we hold, not a sample
    drawn from a larger one.
    """
    by_category = defaultdict(list)
    for receipt in _spending(receipts):
        if receipt.total_incl_tax_cents:
            by_category[receipt.category or 'uncategorised'].append(receipt)

    findings = []
    for category, group in by_category.items():
        if len(group) < min_history:
            continue

        amounts = [receipt.total_incl_tax_cents for receipt in group]
        average, spread = mean(amounts), pstdev(amounts)
        if spread <= 0:
            continue

        for receipt in group:
            deviations = (receipt.total_incl_tax_cents - average) / spread
            if deviations < sigma:
                continue
            findings.append({
                'receipt_id': receipt.id,
                'vendor_name': _vendor_name(receipt),
                'category': category.replace('_', ' '),
                'amount_cents': receipt.total_incl_tax_cents,
                'typical_cents': int(average),
                'deviations': round(deviations, 1),
                'compared_with': len(group),
                'on': receipt.receipt_date,
            })

    findings.sort(key=lambda finding: finding['deviations'], reverse=True)
    return findings[:limit]


def vendor_upload_behaviour(receipts, limit=8):
    """
    Which suppliers keep us waiting for a receipt to appear on the TRA portal.

    Measured from what was actually observed: a submission that needed no retry proves
    the receipt was already on the portal, and one that needed retries proves it was
    not there when first checked and appeared some time later. The delay between the
    first attempt and the successful one is that wait, and it is a lower bound rather
    than an exact figure - the receipt may have appeared at any point in between.

    A supplier that never fails a first check does not appear here at all.
    """
    by_vendor = defaultdict(lambda: {'total': 0, 'late': 0, 'waits': []})
    for receipt in receipts:
        vendor = _vendor_key(receipt)
        submission = receipt.submission
        if vendor is None or submission is None:
            continue

        profile = by_vendor[vendor]
        profile['total'] += 1
        profile['name'] = _vendor_name(receipt)

        if not submission.retry_count:
            continue

        profile['late'] += 1
        if receipt.processed_at and submission.received_at:
            waited = (receipt.processed_at - submission.received_at).total_seconds() / 3600
            profile['waits'].append(max(0.0, waited))

    findings = [
        {
            'vendor_name': profile['name'],
            'receipts': profile['total'],
            'late': profile['late'],
            'late_pct': round(profile['late'] * 100 / profile['total'], 1),
            'median_wait_hours': round(_median(profile['waits']), 1) if profile['waits'] else None,
        }
        for profile in by_vendor.values() if profile['late']
    ]
    findings.sort(key=lambda finding: (finding['late_pct'], finding['late']), reverse=True)
    return findings[:limit]


def verification_reliability(receipts, submissions, limit=8):
    """
    Which suppliers' receipts give TRA trouble, and how much cannot be pinned on anyone.

    The obstacle is that a submission which never verified has no receipt, and so no
    vendor: TRA is the only thing that would have told us who issued it. What breaks
    the deadlock is the verification code, which is read off the submitted URL at
    intake and is the receipt's identity. Any submission carrying a code that a stored
    receipt also carries belongs to that receipt's vendor - whether that submission
    succeeded, needed retries, or gave up entirely and was submitted again later.

    Two figures come out of it, and conflating them would be the whole mistake:

      * per vendor, how often their receipts are not verifiable on the first attempt;
      * the submissions that never resolved and whose code matches nothing we hold.
        Those cannot be attributed to anybody, and reporting them as a vendor's fault
        would be inventing a fact. They are counted separately, and listed, because
        each one is a receipt somebody still has to chase by hand.

    `submissions` should be every submission in the period, not only the failed ones -
    a failure rate needs its denominator.
    """
    # The code is the join. Built from receipts we actually hold, so a code that never
    # verified anywhere simply is not in here.
    vendor_by_code = {}
    for receipt in receipts:
        if receipt.receipt_verification_code:
            vendor_by_code[receipt.receipt_verification_code] = {
                'key': _vendor_key(receipt), 'name': _vendor_name(receipt),
            }

    profiles = defaultdict(lambda: {'attempts': 0, 'clean': 0, 'retried': 0, 'gave_up': 0})
    unattributed = []

    for submission in submissions:
        if submission.input_type != 'url':
            continue

        vendor = vendor_by_code.get(submission.receipt_code) if submission.receipt_code else None
        if vendor is None:
            # Only a submission that never resolved is a problem worth reporting; one
            # still working through its retries has not failed yet.
            if submission.status == 'failed':
                unattributed.append({
                    'submission_id': submission.id,
                    'receipt_code': submission.receipt_code,
                    'failure_reason': submission.failure_reason,
                    'received_at': submission.received_at,
                })
            continue

        profile = profiles[vendor['key']]
        profile['name'] = vendor['name']
        profile['attempts'] += 1
        if submission.status == 'failed':
            profile['gave_up'] += 1
        elif submission.retry_count:
            profile['retried'] += 1
        else:
            profile['clean'] += 1

    findings = [
        {
            'vendor_name': profile['name'],
            'attempts': profile['attempts'],
            'clean': profile['clean'],
            'retried': profile['retried'],
            'gave_up': profile['gave_up'],
            'trouble_pct': round((profile['retried'] + profile['gave_up']) * 100 / profile['attempts'], 1),
        }
        for profile in profiles.values()
        if profile['retried'] or profile['gave_up']
    ]
    findings.sort(key=lambda finding: (finding['trouble_pct'], finding['attempts']), reverse=True)

    unattributed.sort(key=lambda entry: entry['received_at'], reverse=True)
    return {
        'vendors': findings[:limit],
        'unattributed': unattributed[:limit],
        'unattributed_total': len(unattributed),
    }


def location_outliers(receipts, threshold_km=LOCATION_OUTLIER_KM,
                      min_history=MIN_LOCATION_HISTORY, limit=8):
    """
    Receipts collected a long way from where this supplier's receipts normally come.

    A supplier with branches will produce these legitimately, which is why the
    threshold is generous and the finding names the distance rather than a verdict.
    What it is really looking for is a receipt collected somewhere the business was
    not - the shape that a receipt picked up off the street makes.
    """
    by_vendor = defaultdict(list)
    for receipt in _spending(receipts):
        vendor = _vendor_key(receipt)
        point = parse_location(receipt.submission.location) if receipt.submission else None
        if vendor and point:
            by_vendor[vendor].append((receipt, point))

    findings = []
    for located in by_vendor.values():
        if len(located) < min_history:
            continue

        for receipt, point in located:
            # Compared against where the *other* receipts came from, so a single
            # distant receipt cannot drag the centre towards itself and hide.
            others = [other for other_receipt, other in located if other_receipt.id != receipt.id]
            usual = median_point(others)
            separation = distance_km(point, usual)
            if separation is None or separation < threshold_km:
                continue

            findings.append({
                'receipt_id': receipt.id,
                'vendor_name': _vendor_name(receipt),
                'distance_km': int(separation),
                'region': region_for(point),
                'usual_region': region_for(usual),
                'compared_with': len(others),
                'on': receipt.receipt_date,
            })

    findings.sort(key=lambda finding: finding['distance_km'], reverse=True)
    return findings[:limit]


def category_breakdown(receipts):
    """Spend per expense category, largest first, with each one's share."""
    totals = defaultdict(lambda: {'cents': 0, 'count': 0})
    for receipt in _spending(receipts):
        entry = totals[receipt.category or 'uncategorised']
        entry['cents'] += receipt.total_incl_tax_cents or 0
        entry['count'] += 1

    overall = sum(entry['cents'] for entry in totals.values())
    breakdown = [
        {
            'category': name.replace('_', ' '),
            'key': name,
            'cents': entry['cents'],
            'count': entry['count'],
            'share_pct': round(entry['cents'] * 100 / overall, 1) if overall else 0.0,
        }
        for name, entry in totals.items()
    ]
    breakdown.sort(key=lambda entry: entry['cents'], reverse=True)
    return breakdown


def region_breakdown(receipts):
    """
    Spend per region, from the coordinates the submitting device attached.

    Receipts with no usable location are reported together under 'Unknown' rather
    than dropped, so the shares always add up to the money actually spent.
    """
    totals = defaultdict(lambda: {'cents': 0, 'count': 0, 'points': []})
    for receipt in _spending(receipts):
        point = parse_location(receipt.submission.location) if receipt.submission else None
        name = region_for(point) or 'Unknown'
        entry = totals[name]
        entry['cents'] += receipt.total_incl_tax_cents or 0
        entry['count'] += 1
        if point:
            entry['points'].append({'point': point, 'receipt_id': receipt.id})

    overall = sum(entry['cents'] for entry in totals.values())
    breakdown = [
        {
            'region': name,
            'cents': entry['cents'],
            'count': entry['count'],
            'share_pct': round(entry['cents'] * 100 / overall, 1) if overall else 0.0,
            'points': entry['points'],
        }
        for name, entry in totals.items()
    ]
    # Largest first, but 'Unknown' always last: it is an absence of data, not a place.
    breakdown.sort(key=lambda entry: (entry['region'] == 'Unknown', -entry['cents']))
    return breakdown


def monthly_totals(receipts, months=6, today=None):
    """The last `months` calendar months of spending, oldest first."""
    today = today or date.today()
    buckets = []
    year, month = today.year, today.month
    for _ in range(months):
        buckets.append((year, month))
        year, month = (year - 1, 12) if month == 1 else (year, month - 1)
    buckets.reverse()

    totals = {bucket: 0 for bucket in buckets}
    for receipt in _spending(receipts):
        if receipt.receipt_date is None:
            continue
        bucket = (receipt.receipt_date.year, receipt.receipt_date.month)
        if bucket in totals:
            totals[bucket] += receipt.total_incl_tax_cents or 0

    return [
        {'label': f'{year:04d}-{month:02d}', 'cents': totals[(year, month)]}
        for year, month in buckets
    ]


def _median(values):
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2
