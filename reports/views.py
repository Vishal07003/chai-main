from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import permissions, status
from rest_framework.exceptions import ValidationError, NotAcceptable
from rest_framework.renderers import BaseRenderer
from rest_framework.negotiation import DefaultContentNegotiation
from django.http import HttpResponse

from accounts.permissions import IsOwnerUser
from .utils import resolve_date_range
from .engine import ReportsEngine
from .pdf_generator import generate_report_pdf
from .excel_generator import generate_report_excel


class ForgivingContentNegotiation(DefaultContentNegotiation):
    """
    Ensures binary export endpoints always serve the requested binary payload
    without failing with HTTP 406 Not Acceptable when clients (such as default
    Axios / Fetch / RN clients) send Accept: application/json or general wildcards.
    """
    def select_renderer(self, request, renderers, format_suffix=None):
        try:
            return super().select_renderer(request, renderers, format_suffix)
        except NotAcceptable:
            return (renderers[0], renderers[0].media_type)


class PassthroughBinaryRenderer(BaseRenderer):
    """
    Renders raw binary data for DRF content negotiation.
    Matches the specific media_type or wildcard requests.
    """
    charset = None
    render_style = 'binary'

    def render(self, data, accepted_media_type=None, renderer_context=None):
        return data


class PassthroughPdfRenderer(PassthroughBinaryRenderer):
    media_type = 'application/pdf'
    format = 'pdf'


class PassthroughExcelRenderer(PassthroughBinaryRenderer):
    media_type = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    format = 'xlsx'


class BaseReportView(APIView):
    permission_classes = [permissions.IsAuthenticated, IsOwnerUser]

    def get_range_info(self, request):
        return resolve_date_range(request.query_params)


class ReportOverviewView(BaseReportView):
    def get(self, request):
        range_info = self.get_range_info(request)
        data = ReportsEngine.get_overview(range_info)
        return Response(data)


class ReportSalesView(BaseReportView):
    def get(self, request):
        range_info = self.get_range_info(request)
        data = ReportsEngine.get_sales_report(range_info)
        return Response(data)


class ReportCollectionsView(BaseReportView):
    def get(self, request):
        range_info = self.get_range_info(request)
        data = ReportsEngine.get_collections_report(range_info)
        return Response(data)


class ReportOutstandingView(BaseReportView):
    def get(self, request):
        range_info = self.get_range_info(request)
        data = ReportsEngine.get_outstanding_report(range_info)
        return Response(data)


class ReportProductsView(BaseReportView):
    def get(self, request):
        range_info = self.get_range_info(request)
        data = ReportsEngine.get_products_report(range_info)
        return Response(data)


class ReportCustomersView(BaseReportView):
    def get(self, request):
        range_info = self.get_range_info(request)
        data = ReportsEngine.get_customers_report(range_info)
        return Response(data)


class ReportDeliveriesView(BaseReportView):
    def get(self, request):
        range_info = self.get_range_info(request)
        data = ReportsEngine.get_deliveries_report(range_info)
        return Response(data)


class ReportDiscountsView(BaseReportView):
    def get(self, request):
        range_info = self.get_range_info(request)
        data = ReportsEngine.get_discounts_report(range_info)
        return Response(data)


class ReportProfitLossView(BaseReportView):
    def get(self, request):
        range_info = self.get_range_info(request)
        data = ReportsEngine.get_profit_loss_report(range_info)
        return Response(data)


class ReportUnifiedView(BaseReportView):
    """
    Phase R5: Unified Business Report API.
    Returns complete consolidated business and financial metrics in one call.
    """
    def get(self, request):
        range_info = self.get_range_info(request)
        data = ReportsEngine.get_unified_report(range_info)
        return Response(data)


class ReportExportPdfView(BaseReportView):
    renderer_classes = [PassthroughPdfRenderer]
    content_negotiation_class = ForgivingContentNegotiation

    def get(self, request):
        report_type = request.query_params.get('report_type', 'unified').lower().strip()
        range_info = self.get_range_info(request)

        engine_dispatch = {
            'unified': ReportsEngine.get_unified_report,
            'business-report': ReportsEngine.get_unified_report,
            'overview': ReportsEngine.get_overview,
            'sales': ReportsEngine.get_sales_report,
            'collections': ReportsEngine.get_collections_report,
            'outstanding': ReportsEngine.get_outstanding_report,
            'products': ReportsEngine.get_products_report,
            'customers': ReportsEngine.get_customers_report,
            'deliveries': ReportsEngine.get_deliveries_report,
            'discounts': ReportsEngine.get_discounts_report,
            'profit_loss': ReportsEngine.get_profit_loss_report,
            'profit-loss': ReportsEngine.get_profit_loss_report,
        }

        func = engine_dispatch.get(report_type)
        if not func:
            raise ValidationError({'report_type': f"Invalid report_type '{report_type}'. Allowed: {list(engine_dispatch.keys())}"})

        data = func(range_info)
        pdf_bytes = generate_report_pdf(report_type, data)

        filter_type = range_info.get('filter_type', 'custom')
        if filter_type == 'last_30_days':
            period_slug = "Last_30_Days"
        elif filter_type == 'last_month':
            period_slug = "Last_Month"
        elif filter_type == 'last_3_months':
            period_slug = "Last_3_Months"
        elif filter_type == 'custom':
            period_slug = f"Custom_{range_info['from_date']}_to_{range_info['to_date']}"
        else:
            period_slug = f"{filter_type}_{range_info['from_date']}"

        if report_type in ['unified', 'business-report']:
            filename = f"ChaiFlow_Business_Report_{period_slug}.pdf"
        else:
            filename = f"ChaiFlow_Report_{report_type}_{range_info['from_date']}.pdf"

        response = HttpResponse(pdf_bytes, content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        response['Content-Length'] = len(pdf_bytes)
        return response


class ReportExportExcelView(BaseReportView):
    renderer_classes = [PassthroughExcelRenderer]
    content_negotiation_class = ForgivingContentNegotiation

    def get(self, request):
        report_type = request.query_params.get('report_type', 'unified').lower().strip()
        range_info = self.get_range_info(request)

        engine_dispatch = {
            'unified': ReportsEngine.get_unified_report,
            'business-report': ReportsEngine.get_unified_report,
            'overview': ReportsEngine.get_overview,
            'sales': ReportsEngine.get_sales_report,
            'collections': ReportsEngine.get_collections_report,
            'outstanding': ReportsEngine.get_outstanding_report,
            'products': ReportsEngine.get_products_report,
            'customers': ReportsEngine.get_customers_report,
            'deliveries': ReportsEngine.get_deliveries_report,
            'discounts': ReportsEngine.get_discounts_report,
            'profit_loss': ReportsEngine.get_profit_loss_report,
            'profit-loss': ReportsEngine.get_profit_loss_report,
        }

        func = engine_dispatch.get(report_type)
        if not func:
            raise ValidationError({'report_type': f"Invalid report_type '{report_type}'. Allowed: {list(engine_dispatch.keys())}"})

        data = func(range_info)
        excel_bytes = generate_report_excel(report_type, data)

        filter_type = range_info.get('filter_type', 'custom')
        if filter_type == 'last_30_days':
            period_slug = "Last_30_Days"
        elif filter_type == 'last_month':
            period_slug = "Last_Month"
        elif filter_type == 'last_3_months':
            period_slug = "Last_3_Months"
        elif filter_type == 'custom':
            period_slug = f"Custom_{range_info['from_date']}_to_{range_info['to_date']}"
        else:
            period_slug = f"{filter_type}_{range_info['from_date']}"

        if report_type in ['unified', 'business-report']:
            filename = f"ChaiFlow_Business_Report_{period_slug}.xlsx"
        else:
            filename = f"ChaiFlow_Report_{report_type}_{range_info['from_date']}.xlsx"

        response = HttpResponse(excel_bytes, content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        response['Content-Length'] = len(excel_bytes)
        return response
