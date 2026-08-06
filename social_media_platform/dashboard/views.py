from datetime import timedelta
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User
from django.db.models import Case, Count, DecimalField, F, Q, Sum, Value, When
from django.db.models.functions import Coalesce, TruncMonth
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from posts.models import Post
from subscriptions.models import Plan, UserSubscription

from .decorators import superadmin_required


def _purchase_amount_annotation():
    """Prefer recorded payment amount, else plan price (trials count as 0)."""
    return Case(
        When(latest_payment_amount__isnull=False, then=F('latest_payment_amount')),
        When(is_trial=True, then=Value(Decimal('0.00'))),
        default=F('plan__price'),
        output_field=DecimalField(max_digits=12, decimal_places=2),
    )


def _subscription_analytics(now=None):
    """Build subscriber + purchase amount analytics for the admin overview."""
    now = now or timezone.now()
    amount = _purchase_amount_annotation()
    subs = UserSubscription.objects.select_related('plan', 'user').annotate(purchase_amount=amount)

    total_purchases = subs.aggregate(
        total=Coalesce(Sum('purchase_amount'), Value(Decimal('0.00')), output_field=DecimalField()),
        count=Count('id'),
    )
    active_subs = subs.filter(status='active', expiry_date__gt=now)
    paid_active = active_subs.filter(is_trial=False)
    trial_active = active_subs.filter(is_trial=True)

    unique_subscribers = (
        UserSubscription.objects.values('user_id').distinct().count()
    )
    unique_active_subscribers = (
        active_subs.values('user_id').distinct().count()
    )

    by_plan = []
    for plan in Plan.objects.order_by('price', 'name'):
        plan_subs = UserSubscription.objects.filter(plan=plan).annotate(purchase_amount=amount)
        agg = plan_subs.aggregate(
            total_subs=Count('id'),
            active_subs=Count('id', filter=Q(status='active', expiry_date__gt=now)),
            revenue=Coalesce(
                Sum('purchase_amount'),
                Value(Decimal('0.00')),
                output_field=DecimalField(max_digits=12, decimal_places=2),
            ),
        )
        by_plan.append({
            'name': plan.name,
            'plan_type': plan.get_plan_type_display(),
            'price': plan.price,
            'total_subs': agg['total_subs'] or 0,
            'active_subs': agg['active_subs'] or 0,
            'revenue': agg['revenue'] or Decimal('0.00'),
        })

    monthly_qs = (
        UserSubscription.objects.annotate(
            purchase_amount=amount,
            period=TruncMonth('start_date'),
        )
        .values('period')
        .annotate(
            subscribers=Count('id'),
            unique_users=Count('user_id', distinct=True),
            revenue=Coalesce(
                Sum('purchase_amount'),
                Value(Decimal('0.00')),
                output_field=DecimalField(max_digits=12, decimal_places=2),
            ),
        )
    )
    monthly_map = {}
    for row in monthly_qs:
        if not row['period']:
            continue
        p = row['period'].date() if hasattr(row['period'], 'date') else row['period']
        monthly_map[(p.year, p.month)] = row

    # Build Jan–Dec series for each available year (for year selector)
    years_with_data = sorted({y for (y, _m) in monthly_map.keys()}, reverse=True)
    if now.year not in years_with_data:
        years_with_data = [now.year] + years_with_data
    # Always include a small window of recent years even if empty
    for y in range(now.year, now.year - 5, -1):
        if y not in years_with_data:
            years_with_data.append(y)
    years_with_data = sorted(set(years_with_data), reverse=True)

    month_names = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
    by_year = {}
    for year in years_with_data:
        labels = []
        revenue = []
        subscribers = []
        users = []
        for month in range(1, 13):
            labels.append(month_names[month - 1])
            row = monthly_map.get((year, month))
            if row:
                revenue.append(float(row['revenue'] or 0))
                subscribers.append(row['subscribers'] or 0)
                users.append(row['unique_users'] or 0)
            else:
                revenue.append(0.0)
                subscribers.append(0)
                users.append(0)
        by_year[str(year)] = {
            'labels': labels,
            'revenue': revenue,
            'subscribers': subscribers,
            'users': users,
        }

    active_revenue = active_subs.aggregate(
        total=Coalesce(
            Sum('purchase_amount'),
            Value(Decimal('0.00')),
            output_field=DecimalField(max_digits=12, decimal_places=2),
        )
    )['total'] or Decimal('0.00')

    charts = {
        'default_year': str(now.year),
        'years': [str(y) for y in years_with_data],
        'by_year': by_year,
        'plans': {
            'labels': [r['name'] for r in by_plan],
            'revenue': [float(r['revenue'] or 0) for r in by_plan],
            'subscribers': [r['total_subs'] for r in by_plan],
            'active': [r['active_subs'] for r in by_plan],
        },
    }

    return {
        'total_purchase_amount': total_purchases['total'] or Decimal('0.00'),
        'total_subscription_records': total_purchases['count'] or 0,
        'unique_subscribers': unique_subscribers,
        'unique_active_subscribers': unique_active_subscribers,
        'active_paid': paid_active.count(),
        'active_trials': trial_active.count(),
        'active_revenue': active_revenue,
        'by_plan': by_plan,
        'charts': charts,
    }


def admin_root_view(request):
    """/admin/ → overview when superadmin is logged in, otherwise login page."""
    if request.user.is_authenticated and request.user.is_superuser:
        return redirect('dashboard:home')
    return admin_login_view(request)


def admin_login_view(request):
    if request.user.is_authenticated and request.user.is_superuser:
        return redirect('dashboard:home')

    error = None
    if request.method == 'POST':
        email = request.POST.get('email', '').strip()
        password = request.POST.get('password', '')
        user = None

        if email and password:
            matched = User.objects.filter(email__iexact=email).order_by('-is_superuser', 'id').first()
            if matched:
                user = authenticate(request, username=matched.username, password=password)

        if user is None:
            error = 'Invalid email or password.'
        elif not user.is_superuser:
            error = 'Access denied. Superadmin credentials required.'
        else:
            login(request, user)
            messages.success(request, f'Welcome back, {user.get_full_name() or user.email}.')
            return redirect('dashboard:home')

    return render(request, 'dashboard/login.html', {'error': error})


@require_POST
def admin_logout_view(request):
    logout(request)
    messages.info(request, 'You have been signed out of the admin panel.')
    return redirect('dashboard:login')


@superadmin_required
def admin_home_view(request):
    """Platform usage overview."""
    from posts.publisher import publish_due_posts

    publish_due_posts()

    now = timezone.now()
    users_qs = User.objects.filter(is_superuser=False)
    posts_qs = Post.objects.all()
    subs_qs = UserSubscription.objects.all()

    stats = {
        'total_users': users_qs.count(),
        'active_users': users_qs.filter(is_active=True).count(),
        'inactive_users': users_qs.filter(is_active=False).count(),
        'active_subs': subs_qs.filter(status='active', expiry_date__gt=now).count(),
        'expired_subs': subs_qs.filter(Q(status='expired') | Q(expiry_date__lte=now)).count(),
        'total_posts': posts_qs.count(),
        'scheduled_posts': posts_qs.filter(status=Post.STATUS_SCHEDULED).count(),
        'published_posts': posts_qs.filter(status=Post.STATUS_PUBLISHED).count(),
        'failed_posts': posts_qs.filter(status=Post.STATUS_FAILED).count(),
        'active_plans': Plan.objects.filter(is_active=True).count(),
    }

    recent_users = users_qs.order_by('-date_joined')[:6]
    recent_posts = posts_qs.select_related('user').order_by('-created_at')[:8]
    recent_subs = subs_qs.select_related('user', 'plan').order_by('-start_date')[:6]
    analytics = _subscription_analytics(now)

    return render(request, 'dashboard/home.html', {
        'stats': stats,
        'analytics': analytics,
        'recent_users': recent_users,
        'recent_posts': recent_posts,
        'recent_subs': recent_subs,
    })


@superadmin_required
def users_list_view(request):
    q = request.GET.get('q', '').strip()
    status = request.GET.get('status', '').strip()
    users = (
        User.objects.filter(is_superuser=False)
        .annotate(sub_count=Count('subscriptions'), post_count=Count('posts'))
        .order_by('-date_joined')
    )
    if status == 'active':
        users = users.filter(is_active=True)
    elif status == 'inactive':
        users = users.filter(is_active=False)
    if q:
        users = users.filter(
            Q(username__icontains=q)
            | Q(email__icontains=q)
            | Q(first_name__icontains=q)
            | Q(last_name__icontains=q)
        )

    user_rows = []
    for user in users:
        current = (
            UserSubscription.objects.filter(user=user, status='active')
            .select_related('plan')
            .order_by('-start_date')
            .first()
        )
        user_rows.append({
            'user': user,
            'subscription': current,
            'sub_count': user.sub_count,
            'post_count': user.post_count,
        })

    return render(request, 'dashboard/users_list.html', {
        'user_rows': user_rows,
        'q': q,
        'status': status,
        'total': len(user_rows),
    })


@superadmin_required
def user_detail_view(request, user_id):
    user = get_object_or_404(User, pk=user_id, is_superuser=False)
    subscriptions = (
        UserSubscription.objects.filter(user=user)
        .select_related('plan')
        .order_by('-start_date')
    )
    current = next((s for s in subscriptions if s.is_valid), None)
    profile = getattr(user, 'profile', None)
    plans = Plan.objects.filter(is_active=True).order_by('price', 'name')
    user_posts = Post.objects.filter(user=user).order_by('-created_at')[:10]

    return render(request, 'dashboard/user_detail.html', {
        'target_user': user,
        'profile': profile,
        'subscriptions': subscriptions,
        'current_subscription': current,
        'plans': plans,
        'user_posts': user_posts,
    })


@superadmin_required
@require_POST
def user_toggle_active_view(request, user_id):
    user = get_object_or_404(User, pk=user_id, is_superuser=False)
    user.is_active = not user.is_active
    user.save(update_fields=['is_active'])
    state = 'activated' if user.is_active else 'deactivated'
    messages.success(request, f'User {user.get_full_name() or user.email} has been {state}.')
    next_url = request.POST.get('next') or ''
    if next_url.startswith('/admin/'):
        return redirect(next_url)
    return redirect('dashboard:user_detail', user_id=user.id)


@superadmin_required
@require_POST
def assign_subscription_view(request, user_id):
    user = get_object_or_404(User, pk=user_id, is_superuser=False)
    plan_id = request.POST.get('plan_id', '').strip()
    duration_days = request.POST.get('duration_days', '').strip()
    notes = request.POST.get('notes', '').strip()

    plan = get_object_or_404(Plan, pk=plan_id, is_active=True)
    try:
        days = int(duration_days) if duration_days else plan.duration_days
        if days < 1:
            raise ValueError
    except ValueError:
        messages.error(request, 'Duration must be a positive number of days.')
        return redirect('dashboard:user_detail', user_id=user.id)

    UserSubscription.objects.filter(user=user, status='active').update(status='inactive')

    sub = UserSubscription.objects.create(
        user=user,
        plan=plan,
        status='active',
        expiry_date=timezone.now() + timedelta(days=days),
        is_trial=(plan.plan_type == 'free_trial'),
        notes=notes or f'Manually assigned by {request.user.get_full_name() or request.user.email}',
        latest_payment_status='manual_admin',
        latest_payment_date=timezone.now(),
        latest_payment_amount=plan.price,
        latest_payment_currency='usd',
    )
    messages.success(
        request,
        f'Assigned {plan.name} to {user.get_full_name() or user.email} until {sub.expiry_date:%b %d, %Y}.',
    )
    return redirect('dashboard:subscription_detail', sub_id=sub.id)


@superadmin_required
def subscriptions_list_view(request):
    q = request.GET.get('q', '').strip()
    status = request.GET.get('status', '').strip()
    user_id = request.GET.get('user', '').strip()

    subs = UserSubscription.objects.select_related('user', 'plan').order_by('-start_date')
    filter_user = None

    if user_id.isdigit():
        filter_user = User.objects.filter(pk=int(user_id), is_superuser=False).first()
        if filter_user:
            subs = subs.filter(user=filter_user)

    if status:
        subs = subs.filter(status=status)
    if q:
        subs = subs.filter(
            Q(user__username__icontains=q)
            | Q(user__email__icontains=q)
            | Q(plan__name__icontains=q)
            | Q(stripe_customer_id__icontains=q)
            | Q(stripe_subscription_id__icontains=q)
            | Q(latest_invoice_id__icontains=q)
        )

    return render(request, 'dashboard/subscriptions_list.html', {
        'subscriptions': subs,
        'q': q,
        'status': status,
        'filter_user': filter_user,
        'status_choices': UserSubscription.STATUS_CHOICES,
        'total': subs.count(),
    })


@superadmin_required
def subscription_detail_view(request, sub_id):
    subscription = get_object_or_404(
        UserSubscription.objects.select_related('user', 'plan'),
        pk=sub_id,
    )
    user = subscription.user
    profile = getattr(user, 'profile', None)
    other_subs = (
        UserSubscription.objects.filter(user=user)
        .exclude(pk=subscription.pk)
        .select_related('plan')
        .order_by('-start_date')[:5]
    )

    return render(request, 'dashboard/subscription_detail.html', {
        'subscription': subscription,
        'target_user': user,
        'profile': profile,
        'other_subs': other_subs,
    })


@superadmin_required
def posts_list_view(request):
    from posts.publisher import publish_due_posts

    publish_due_posts()

    q = request.GET.get('q', '').strip()
    status = request.GET.get('status', '').strip()
    user_id = request.GET.get('user', '').strip()

    posts = Post.objects.select_related('user').order_by('-created_at')
    filter_user = None

    if status in {Post.STATUS_SCHEDULED, Post.STATUS_PUBLISHED, Post.STATUS_DRAFT, Post.STATUS_FAILED}:
        posts = posts.filter(status=status)
    if user_id.isdigit():
        filter_user = User.objects.filter(pk=int(user_id), is_superuser=False).first()
        if filter_user:
            posts = posts.filter(user=filter_user)
    if q:
        posts = posts.filter(
            Q(description__icontains=q)
            | Q(caption__icontains=q)
            | Q(user__username__icontains=q)
            | Q(image_prompt__icontains=q)
        )

    return render(request, 'dashboard/posts_list.html', {
        'posts': posts[:200],
        'q': q,
        'status': status,
        'filter_user': filter_user,
        'total': posts.count(),
        'status_choices': Post.STATUS_CHOICES,
    })


@superadmin_required
def post_detail_view(request, post_id):
    post = get_object_or_404(Post.objects.select_related('user'), pk=post_id)
    return render(request, 'dashboard/post_detail.html', {'post': post})
