from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import permissions, status
from rest_framework.exceptions import ValidationError
from rest_framework.renderers import BaseRenderer
from django.http import HttpResponse

from accounts.permissions import IsOwnerUser
from .utils import resolve_date_range
from .engine import ReportsEngine
from .pdf_generator import generate_report_pdf
from .excel_generator import generate_report_excel


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


class ReportExportPdfView(BaseReportView):
    renderer_classes = [PassthroughPdfRenderer]

    def get(self, request):
        report_type = request.query_params.get('report_type', 'sales').lower().strip()
        range_info = self.get_range_info(request)

        engine_dispatch = {
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

        filename = f"ChaiFlow_Report_{report_type}_{range_info['from_date']}.pdf"
        response = HttpResponse(pdf_bytes, content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        response['Content-Length'] = len(pdf_bytes)
        return response


class ReportExportExcelView(BaseReportView):
    renderer_classes = [PassthroughExcelRenderer]

    def get(self, request):
        report_type = request.query_params.get('report_type', 'sales').lower().strip()
        range_info = self.get_range_info(request)

        engine_dispatch = {
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

        filename = f"ChaiFlow_Report_{report_type}_{range_info['from_date']}.xlsx"
        response = HttpResponse(excel_bytes, content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        response['Content-Length'] = len(excel_bytes)
        return response
