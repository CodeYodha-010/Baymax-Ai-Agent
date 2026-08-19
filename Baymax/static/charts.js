window.BaymaxCharts = (function() {
    const COLORS = ['#4e73df','#36b9cc','#f6c23e','#e74a33','#9632db','#17bd97','#f15f75','#5981ea'];
    const _instances = {};
    let _modalChart = null;

    function destroy(containerId) {
        if (_instances[containerId]) {
            _instances[containerId].destroy();
            delete _instances[containerId];
        }
        const el = document.getElementById(containerId);
        if (el) el.innerHTML = '';
    }

    function buildHighchartsOpts(chartConfig, opts) {
        opts = opts || {};
        var onPointClick = opts.onPointClick || null;
        const typeMap = {
            bar: 'column', line: 'line', pie: 'pie', scatter: 'scatter',
            histogram: 'column', area: 'area', heatmap: 'heatmap', candlestick: 'candlestick'
        };
        const hcType = typeMap[chartConfig.chartType] || 'column';
        let series = chartConfig.datasets;

        if (hcType === 'candlestick') {
            // Candlestick data format: [open, high, low, close] per point
            series = [{
                name: chartConfig.datasets && chartConfig.datasets[0] ? chartConfig.datasets[0].label : 'OHLC',
                type: 'candlestick',
                data: chartConfig.datasets && chartConfig.datasets[0] ? chartConfig.datasets[0].data : [],
                color: '#e74a3b',
                upColor: '#28a745',
                lineColor: '#e74a3b',
                upLineColor: '#28a745'
            }];
        } else if (hcType === 'pie') {
            series = [{
                name: chartConfig.datasets && chartConfig.datasets[0] ? chartConfig.datasets[0].label : 'Value',
                colorByPoint: true,
                data: (chartConfig.labels || []).map(function(label, i) {
                    return {
                        name: String(label),
                        y: chartConfig.datasets && chartConfig.datasets[0] ? chartConfig.datasets[0].data[i] : 0
                    };
                })
            }];
        }

        var chartOpts = {
            chart: {
                type: hcType,
                styledMode: false,
                height: opts.height || null,
                zoomType: 'xy',
                panning: true,
                panKey: 'shift',
                resetZoomButton: {
                    position: { align: 'right', x: -10, y: 10 },
                    theme: { fill: '#4e73df', stroke: '#2e59d9', style: { color: '#fff', borderRadius: '4px' } }
                }
            },
            title: { text: chartConfig.title || '' },
            legend: {
                enabled: true,
                layout: 'horizontal',
                align: 'center',
                verticalAlign: 'bottom',
                itemStyle: { fontSize: '12px', fontWeight: 'normal' },
                itemHoverStyle: { color: '#4e73df' }
            },
            xAxis: (hcType === 'pie' || hcType === 'candlestick') ? { visible: false } : {
                categories: chartConfig.labels || [],
                crosshair: true,
                labels: { style: { fontSize: '11px' } },
                title: { text: chartConfig.xAxisLabel || '' }
            },
            yAxis: hcType !== 'pie' ? {
                title: { text: chartConfig.datasets && chartConfig.datasets[0] ? chartConfig.datasets[0].label : '' },
                crosshair: true,
                labels: { style: { fontSize: '11px' } }
            } : {},
            tooltip: hcType === 'candlestick' ? {
                useHTML: true,
                style: { fontSize: '12px' },
                pointFormat: '<span style="color:{point.color}">\u25CF</span> Open: <b>{point.open}</b><br/>' +
                             '<span style="color:{point.color}">\u25CF</span> High: <b>{point.high}</b><br/>' +
                             '<span style="color:{point.color}">\u25CF</span> Low: <b>{point.low}</b><br/>' +
                             '<span style="color:{point.color}">\u25CF</span> Close: <b>{point.close}</b><br/>'
            } : {
                shared: true,
                crosshairs: true,
                useHTML: true,
                style: { fontSize: '12px' },
                headerFormat: '<span style="font-weight:600;font-size:12px;">{point.key}</span><br/>',
                pointFormat: '<span style="color:{point.color}">\u25CF</span> {series.name}: <b>{point.y}</b><br/>'
            },
            series: series,
            colors: COLORS,
            credits: { enabled: false },
            exporting: {
                enabled: !!opts.showExport,
                buttons: {
                    contextButton: {
                        menuItems: ['downloadPNG', 'downloadJPEG', 'downloadSVG', 'downloadPDF', 'separator', 'printChart']
                    }
                }
            },
            plotOptions: {
                series: {
                    animation: { duration: opts.noAnimation ? 0 : 800 },
                    cursor: 'pointer',
                    point: {
                        events: {
                            click: function() {
                                if (onPointClick) {
                                    onPointClick(this);
                                }
                            }
                        }
                    }
                },
                bar: { dataLabels: { enabled: true, style: { fontSize: '10px' } } },
                column: { dataLabels: { enabled: true, style: { fontSize: '10px' } } },
                line: { dataLabels: { enabled: false }, marker: { radius: 4 } },
                area: { dataLabels: { enabled: false }, marker: { radius: 4 } },
                pie: {
                    dataLabels: {
                        enabled: true,
                        format: '<b>{point.name}</b>: {point.percentage:.1f}%',
                        style: { fontSize: '11px' }
                    },
                    showInLegend: true
                },
                scatter: { marker: { radius: 5, states: { hover: { radius: 7 } } } }
            }
        };
        return chartOpts;
    }

    function render(containerId, chartConfig) {
        destroy(containerId);
        var container = document.getElementById(containerId);
        if (!container) return;
        var hc = Highcharts.chart(container, buildHighchartsOpts(chartConfig));
        _instances[containerId] = hc;
        return hc;
    }

    function renderCompact(containerId, chartConfig) {
        destroy(containerId);
        var container = document.getElementById(containerId);
        if (!container) return;
        var hc = Highcharts.chart(container, buildHighchartsOpts(chartConfig, { height: 170, noAnimation: false }));
        _instances[containerId] = hc;
        return hc;
    }

    function openModal(chartConfig) {
        closeModal();
        var overlay = document.createElement('div');
        overlay.className = 'chart-modal-overlay';
        overlay.innerHTML =
            '<div class="chart-modal-card">' +
                '<div class="chart-modal-header">' +
                    '<h6>' + escapeHtml(chartConfig.title || 'Chart') + '</h6>' +
                    '<button class="chart-modal-close" aria-label="Close">&times;</button>' +
                '</div>' +
                '<div class="chart-modal-body"><div class="chart-full" id="chartModalBody"></div></div>' +
            '</div>';
        document.body.appendChild(overlay);

        overlay.querySelector('.chart-modal-close').addEventListener('click', closeModal);
        overlay.addEventListener('click', function(e) {
            if (e.target === overlay) closeModal();
        });
        document.addEventListener('keydown', function escHandler(e) {
            if (e.key === 'Escape') { closeModal(); document.removeEventListener('keydown', escHandler); }
        });

        requestAnimationFrame(function() {
            overlay.classList.add('active');
            var hc = Highcharts.chart(
                overlay.querySelector('#chartModalBody'),
                buildHighchartsOpts(chartConfig, { height: null, showExport: true })
            );
            _modalChart = hc;
        });
    }

    function closeModal() {
        if (_modalChart) { _modalChart.destroy(); _modalChart = null; }
        var overlay = document.querySelector('.chart-modal-overlay');
        if (overlay) overlay.remove();
    }

    function renderTable(containerId, payload) {
        var container = document.getElementById(containerId);
        if (!container) return;
        var columns = payload.columns || [];
        var rows = payload.rows || [];
        var html = '<div class="table-responsive"><table class="data-table table table-striped table-bordered"><thead><tr>';
        columns.forEach(function(c) { html += '<th>' + escapeHtml(c) + '</th>'; });
        html += '</tr></thead><tbody>';
        rows.forEach(function(row) {
            html += '<tr>';
            row.forEach(function(cell) { html += '<td>' + escapeHtml(cell == null ? '' : cell) + '</td>'; });
            html += '</tr>';
        });
        html += '</tbody></table></div>';
        container.innerHTML = html;
    }

    function escapeHtml(text) {
        var div = document.createElement('div');
        div.textContent = String(text);
        return div.innerHTML;
    }

    return {
        COLORS: COLORS,
        render: render,
        renderCompact: renderCompact,
        openModal: openModal,
        closeModal: closeModal,
        destroy: destroy,
        renderTable: renderTable
    };
})();
