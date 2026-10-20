"""Build the reviewable Teable design contract; never connects to a database."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TABLES = []


def table(key, name, grain, fields, phase=1, rules=()):
    TABLES.append({'key': key, 'name': name, 'phase': phase, 'grain': grain,
                   'fields': [dict(key=f[0], name=f[1], type=f[2], required=f[3], notes=f[4],
                                   **({'target': f[5]} if len(f) > 5 else {})) for f in fields],
                   'rules': list(rules)})


table('products', '产品档案', '一个品牌下一个正式物料号对应一条 SKU；型号、颜色、轴体等不同料号分别保存。', [
    ('product_key', '产品编码', 'text', True, '稳定业务键：品牌 + 正式料号；Teable 内部记录ID另行生成'),
    ('brand', '品牌', 'singleSelect', True, '雷神 / 机械师；源自产品表所在工作表'),
    ('part_number', '料号', 'text', True, '保留原文；关联键只做首尾空白清理和ASCII大小写统一'),
    ('description_zh', '中文物料描述', 'longText', False, '当前规范名称；历史订单保留其当时的描述'),
    ('description_en', '英文物料描述', 'longText', False, '机械师表L列；其他来源缺失时为空'),
    ('category_group', '主品类', 'singleSelect', False, '笔记本/台式机/显示器/外设等；有多个源标记时待人工确认，不任取第一个'),
    ('source_category_flags', '源品类及业务标签', 'multipleSelect', False, '保留M:S或O:AA的全部标记；信创等业务属性不丢弃'),
    ('category_detail', '细分品类', 'singleSelect', False, '键盘/鼠标/手柄等；未知时不从描述猜测'),
    ('manufacturer', '制造商', 'text', False, '独立于品牌、供应商和ODM保存'),
    ('supplier', '供应商', 'text', False, '机械师表M列'),
    ('odm', 'ODM', 'text', False, '雷神2021后L列'),
    ('carton_spec', '储运箱规', 'text', False, '保留原箱规文本；确认单位后再拆装箱数/尺寸'),
    ('pid_raw', 'PID原文', 'text', False, '十六进制文字；0是有效值，不作为缺失'),
    ('registered_date', '条码申请日期', 'date', False, '仅早期表的申请时间；不代替其他日期'),
    ('effective_date', '条码生效日期', 'date', False, '仅2017后表的生效时间'),
    ('source_updated_date', '源描述维护日期', 'date', False, '较新表H列；区别于Teable系统修改时间'),
    ('lifecycle', '生命周期', 'singleSelect', False, '未知/在售/开发中/停产；不根据年代自动判定'),
    ('notes', '产品备注', 'longText', False, '仅业务备注；凭据页、登录信息禁止导入'),
    ('barcodes', '条码记录', 'linkMany', False, '允许同料号历史/不同包装条码共存', 'barcodes'),
    ('source_rows', '来源行', 'linkMany', True, '每条合并后的产品仍能回溯所有来源行', 'import_rows'),
], rules=['同一品牌+料号唯一；不同EAN不能自动拆成不同SKU。', '缺失料号、保留占位或含非ASCII疑似字符的记录先进入待核对区。'])

table('barcodes', '条码台账', '一条国家条码、包装条码或保留编码对应一条分配记录。', [
    ('barcode_key', '条码记录编码', 'text', True, '内部稳定键；缺失公式值时使用来源行键，不以空EAN生成共享记录'),
    ('barcode_value', '条码', 'text', False, '文本保存，保留前导零；未求值的公式不能当作条码'),
    ('barcode_type', '条码类型', 'singleSelect', True, '国家EAN13 / 储运条码 / 待确认'),
    ('product', '产品', 'linkOne', False, '预留条码或未补齐料号的条码可暂不关联SKU', 'products'),
    ('allocation_state', '分配状态', 'singleSelect', True, '已分配/预留/缺少料号/公式待计算/校验待核对/停用'),
    ('validation_state', '校验状态', 'singleSelect', True, '通过/未验证/长度错误/校验位错误/公式无缓存'),
    ('manufacturer', '制造商', 'text', False, '编码主体与品牌不是同一字段'),
    ('effective_date', '生效日期', 'date', False, '只使用明确给出的日期'),
    ('is_preferred', '当前首选', 'checkbox', False, '同SKU多个EAN时由用户确认，不取最后一行作为当前码'),
    ('source_rows', '来源行', 'linkMany', True, '保留分配、校验和公式来源', 'import_rows'),
], rules=['国家EAN按13位和校验位验证；储运码独立验证，不强制套用EAN13规则。', '多个EAN不自动丢弃；失效、首选和历史有效期需人工确认。'])

table('markets', '市场', '一个销售市场对应一条记录；不按工作表创建独立订单表。', [
    ('market_key', '市场编码', 'text', True, '稳定编码'),
    ('name', '市场名称', 'text', True, '由源工作表标签提取；市场名称不等于客户公司'),
    ('source_region', '源区域分组', 'singleSelect', False, '保留SA/SEA/EU原分组，后续可单独规范'),
    ('source_sheet', '源工作表', 'text', False, '保存原工作表全名'),
])

table('customers', '客户', '一个经确认的客户公司对应一条记录；不能从国家标签编造客户。', [
    ('customer_key', '客户编码', 'text', True, '稳定客户编码'),
    ('customer_name', '客户公司', 'text', True, '源Excel没有独立客户列，第一轮可为空表'),
    ('market', '市场', 'linkOne', False, '同市场允许多个客户', 'markets'),
    ('notes', '客户备注', 'longText', False, '不从订单备注自动抓取联系信息'),
])

table('orders', '订单批次', '一个经确认的PO/PI/业务订单或批次；不以料号、序号或市场拼成真实订单号。', [
    ('order_key', '内部订单编码', 'text', True, '内部编码与客户PO/PI分开'),
    ('external_order_number', '客户PO或PI', 'text', False, '源表未提供，保持空值'),
    ('source_batch_label', '原批次描述', 'text', False, '源B列只作描述，不能默认唯一'),
    ('customer', '客户', 'linkOne', False, '经确认再关联', 'customers'),
    ('market', '市场', 'linkOne', True, '业务归属', 'markets'),
    ('order_date', '确认下单日期', 'date', False, '同批次多行日期不一致时必须核对'),
    ('lines', '订单明细', 'linkMany', False, '一单允许多SKU、多交付计划', 'order_lines'),
    ('grouping_confirmed', '批次归并已确认', 'checkbox', False, '第一轮不自动合并空批次或同日同料号记录'),
])

table('order_lines', '订单明细', '保留每一条源需求/订单/排期行；导入初始96行不做自动合单。', [
    ('line_key', '明细编码', 'text', True, '导入时按文件SHA256+工作表+行号生成幂等来源键'),
    ('order', '订单批次', 'linkOne', False, '待批次归并确认后关联；允许暂缺', 'orders'),
    ('market', '市场', 'linkOne', True, '由工作表确定', 'markets'),
    ('product', '产品', 'linkOne', False, '精确料号关联并验证品牌；品牌冲突时暂缓确认', 'products'),
    ('demand_stage', '业务类型', 'singleSelect', False, '需求/预测/确认订单/待核对；原状态不等同已签订单'),
    ('source_batch', '原批次描述', 'text', False, 'B列原文，空白不擅自向下填充'),
    ('source_brand', '原品牌', 'text', False, 'C列，与产品档案品牌分别保留'),
    ('source_category', '原品类', 'text', False, 'D列，保留源分类以便复核'),
    ('source_part', '原料号', 'text', True, 'E列；不拿型号模糊匹配代替料号'),
    ('source_ean', '原EAN', 'text', False, 'F列；46行缺失可查找档案码，但多码时须确认'),
    ('description_snapshot', '下单时型号描述', 'longText', False, 'H列，不随产品规范名称更新而覆盖'),
    ('customer_requirements', '客户描述及要求', 'longText', False, 'G列，不从型号自动推断语言/轴体/定制要求'),
    ('order_date', '下单日期', 'date', False, 'I列；空值不补为导入时间'),
    ('forecast_date', '预测提交日期', 'date', False, 'J列，与下单日期分开'),
    ('planned_date', '计划交付日期', 'date', False, 'K列；月度视图由日期生成，未知不落入某个月'),
    ('quantity', '需求或订单数量', 'integer', True, 'L列；件数，不代表金额'),
    ('source_picked_up_qty', '原表已提货', 'integer', False, 'M列原标题已发货；用户确认含义是渠道业务从基地/仓库提走'),
    ('source_pending_qty', '原表待提货', 'integer', False, 'N列原标题待发货；保存缓存值及原公式供追溯'),
    ('opening_picked_up_qty', '期初已提货确认数', 'integer', False, '迁移核对后填写；确认之前留空，不以零掩盖未知'),
    ('opening_confirmed', '期初数量已核对', 'checkbox', False, '同时确认历史截点，避免与新提货事件重复累计'),
    ('cutover_at', '期初截点', 'dateTime', False, '实际确认的历史截点；不直接使用上传时间'),
    ('pickup_delta', '后续提货增量', 'rollup', False, '汇总已确认且未作废事件的提货增量', 'fulfillment'),
    ('picked_up_qty', '已提货', 'formula', False, '期初已核对时=期初已提货确认数+后续提货增量；否则为空'),
    ('pending_pickup_qty', '待提货', 'formula', False, '期初已核对时=数量-已提货；否则为空；负数报警，不强制截成零'),
    ('source_status', '原订单状态', 'singleSelect', False, '保留五种原状态；不据此反推数量'),
    ('workflow_stage', '当前业务进度', 'singleSelect', False, '需求登记/预测已提交/备货中/待提货/部分提货/提货完成/客户交付中/客户已签收/取消/待核对'),
    ('responsible', '负责人原文', 'text', False, '按各表表头识别S/T列；不固定某个字母为负责人'),
    ('spares_raw', '备品要求原文', 'text', False, 'S列仅部分表是备品，数值0.02的单位/口径待确认'),
    ('fulfillment', '提货及交付记录', 'linkMany', False, '提货与客户交付是不同事件', 'fulfillment'),
    ('followups', '跟进记录', 'linkMany', False, '逐次记录，避免月度备注覆盖', 'followups'),
    ('review_state', '数据核对状态', 'singleSelect', True, '未核对/需核对/已核对'),
    ('source_row', '来源行', 'linkOne', True, '精确回溯到原文件及行', 'import_rows'),
], rules=['已提货表示渠道业务从基地/仓库提走；不等于国际发运或客户签收。',
          '初始历史数量和状态均保留，22条状态与数量冲突的行不能自动闭环。',
          '全部96行可精确匹配料号；其中1行品牌不一致，需要复核后确认产品链接。'])

table('fulfillment', '提货及交付记录', '一次有明确类型、日期和数量的实物流转事件；允许同明细分批发生。', [
    ('event_key', '事件编码', 'text', True, '稳定唯一编码，重复导入凭据不得重复累计'),
    ('line', '订单明细', 'linkOne', True, '每次事件明确落到一个需求/订单行', 'order_lines'),
    ('event_type', '事件类型', 'singleSelect', True, '基地仓库提货/退回基地仓库/国际发运/客户签收/历史修正'),
    ('event_date', '发生日期', 'date', True, '实际日期，不用计划日期代替'),
    ('quantity', '数量', 'integer', True, '普通流转数量为正；修正需明确增减方向和原因'),
    ('evidence', '凭据', 'attachment', False, '提货单/物流凭据/签收单'),
    ('confirmed', '已确认', 'checkbox', True, '未确认事件不进入数量汇总'),
    ('voided', '已作废', 'checkbox', True, '作废后不参与汇总，保留历史记录'),
    ('pickup_delta', '提货增量', 'formula', False, '已确认且未作废：提货+数量、退回-数量；国际发运和客户签收计0；修正经审核'),
    ('notes', '备注', 'longText', False, '不得从含糊的历史文本自动生成带日期/数量的事件'),
], rules=['只有期初截点之后的新事件计入后续增量，避免历史与新增重复。', '客户签收不再次增加已提货数量。'])

table('followups', '跟进记录', '一条业务备注、提货文字记录或交付文字记录；历史备注可没有精确日期。', [
    ('note_key', '记录编码', 'text', True, '来源文件+行+列或合并区域构成来源键'),
    ('lines', '关联明细', 'linkMany', True, '源合并单元格的一条共享备注可关联多条明细', 'order_lines'),
    ('note_type', '记录类型', 'singleSelect', True, '业务提货记录原文/客户交付记录原文/备注/负责人说明'),
    ('body', '记录内容', 'longText', True, '保留原文，不自动解析为已确认实物流转'),
    ('recorded_date', '业务记录日期', 'date', False, '未知时为空；系统创建时间仅表示导入时间'),
    ('source_period', '原备注期间', 'text', False, '保留9月备注、10月更新等原表头，不擅自补年度'),
    ('source_row', '来源行', 'linkOne', False, '支持共享合并区域来源', 'import_rows'),
])

table('import_batches', '导入批次', '一次被明确接收的源文件快照，不等于业务订单批次。', [
    ('batch_key', '导入编码', 'text', True, '内部唯一键'),
    ('filename', '源文件名', 'text', True, '只保存文件名；源原件在受控存储保存'),
    ('sha256', '文件SHA256', 'text', True, '同内容重复导入可识别'),
    ('imported_at', '导入时间', 'dateTime', True, '系统时间，与业务时间分开'),
    ('mapping_version', '映射版本', 'text', True, '固定版本便于重放/回滚'),
    ('state', '状态', 'singleSelect', True, '预检/待核对/已导入/失败/已回滚'),
    ('source_attachment', '源文件', 'attachment', False, '仅受限权限保存，凭据页不进入业务表'),
])

table('import_rows', '来源行', '一个文件快照中的一条业务源行；不收入隐藏凭据页。', [
    ('row_key', '来源键', 'text', True, 'sha256+sheet+row，技术唯一键；不是稳定业务主键'),
    ('batch', '导入批次', 'linkOne', True, '唯一来源快照', 'import_batches'),
    ('sheet', '工作表', 'text', True, '原工作表名称'),
    ('row_number', '原行号', 'integer', True, 'Excel实际行号，不能使用源序号作唯一键'),
    ('raw_business_values', '业务原值', 'longText', True, '白名单业务列JSON，禁用整行无差别抓取'),
    ('formula_metadata', '公式及缓存说明', 'longText', False, '保留公式/缓存/缺失状态，公式不作为业务值执行'),
    ('review_state', '核对状态', 'singleSelect', True, '通过/待核对/隔离/已解决'),
])

table('review_issues', '待核对事项', '一个可追溯的数据冲突或缺失问题；修正可审计。', [
    ('issue_key', '问题编码', 'text', True, '来源键+规则版本+问题类型，避免重复生成'),
    ('source_row', '来源行', 'linkOne', True, '问题原始出处', 'import_rows'),
    ('issue_type', '问题类型', 'singleSelect', True, '公式无缓存/条码校验/品牌冲突/重复料号多码/状态数量冲突/缺少料号/疑似字符混用'),
    ('description', '说明', 'longText', True, '原值与建议分开，未经确认不改原值'),
    ('state', '处理状态', 'singleSelect', True, '未处理/处理中/已确认/已修正/暂缓'),
    ('resolution', '处理结论', 'longText', False, '记录核对人和依据，保留修正历史'),
])

table('usb_pid', 'USB PID台账', '一个VID/PID用途分配；产品型号级资料不能直接当成料号级唯一键。', [
    ('pid_key', '分配编码', 'text', True, '稳定分配键'),
    ('vid_hex', 'VID十六进制', 'text', False, '保留原文，不未经确认当十进制数转换'),
    ('pid_hex', 'PID十六进制', 'text', True, '保留0；规范化和补位需要确认'),
    ('brand', '品牌', 'singleSelect', False, '原B列'),
    ('category', '品类', 'text', False, '原C列'),
    ('model', '型号原文', 'text', True, '原D列，不能只凭名称自动匹配SKU'),
    ('products', '相关SKU', 'linkMany', False, '经过验证后才能关联，可能覆盖多个SKU', 'products'),
    ('status', '研发状态', 'text', False, '原F列'),
    ('firmware_upgrade', '是否需要升级固件', 'text', False, '原G列'),
    ('driver_compatibility', '旧驱动兼容情况', 'text', False, '原H列'),
], phase=2)

DESIGN = {'version': 'draft-2026-10-20-v1', 'status': 'superseded',
          'superseded_by': 'teable-model-v2.json', 'base_name': '外设产品与海外订单',
          'execution': 'Logical design contract only; not a Teable API request or PostgreSQL migration.',
          'confirmed_terms': {'source_M_已发货': '渠道业务已从基地或仓库提走的数量；新字段名已提货',
                              'source_N_待发货': '新字段名待提货；客户实际交付独立记录'},
          'tables': TABLES,
          'views': [
              {'table': 'order_lines', 'name': '全部需求及订单', 'group_by': ['market', 'workflow_stage']},
              {'table': 'order_lines', 'name': '待提货', 'filter': 'opening_confirmed=true AND pending_pickup_qty>0'},
              {'table': 'order_lines', 'name': '计划交付月度', 'group_by': ['planned_date:month', 'market'], 'unknown_dates': '独立未排期组'},
              {'table': 'order_lines', 'name': '仅确认订单', 'filter': 'demand_stage=确认订单'},
              {'table': 'order_lines', 'name': '待核对', 'filter': 'review_state!=已核对'},
              {'table': 'products', 'name': '按品牌及品类', 'group_by': ['brand', 'category_group', 'category_detail']},
              {'table': 'barcodes', 'name': '条码分配与核对', 'group_by': ['allocation_state', 'validation_state']},
          ],
          'implementation_notes': ['Create tables/fields through Teable UI/API so application metadata stays consistent.',
                                   'Link target IDs, actual formula syntax, rollup settings and permissions are resolved during implementation.',
                                   'Constraints in this draft must be implemented and tested; merely listing a rule does not enforce it.',
                                   'Do not import raw workbooks, private profiles, customer records or credentials into Git.']}


def validate(design):
    keys = {t['key'] for t in design['tables']}
    if len(keys) != len(design['tables']):
        raise ValueError('Duplicate table key')
    for t in design['tables']:
        fields = {f['key'] for f in t['fields']}
        if len(fields) != len(t['fields']):
            raise ValueError(f'Duplicate field in {t["key"]}')
        for f in t['fields']:
            if 'target' in f and f['target'] not in keys:
                raise ValueError(f'Unknown link target {f["target"]}')
    for view in design['views']:
        if view['table'] not in keys:
            raise ValueError('View table missing')


def main():
    validate(DESIGN)
    path = ROOT / 'schema/teable-design.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(DESIGN, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    lines = ['# V1字段字典（已被替代）', '',
             '历史设计，仅供追溯。后续实施使用 [V2模型](V2_MODEL.md) 和 [V2字段字典](V2_FIELD_DICTIONARY.md)。', '',
             '版本：`draft-2026-10-20-v1`。不会直接创建表或修改数据库。',
             '', '用户已确认：原表“已发货”指渠道业务从基地/仓库提走，统一改为“已提货”；“待发货”改为“待提货”。', '']
    for t in TABLES:
        lines += [f'## {t["name"]}' + ('（第二阶段）' if t['phase'] == 2 else ''), '', t['grain'], '',
                  '| 字段 | 类型 | 必填 | 来源或规则 |', '| --- | --- | --- | --- |']
        for f in t['fields']:
            kind = f['type'] + (f' → {f["target"]}' if 'target' in f else '')
            lines.append(f'| {f["name"]} | {kind} | {"是" if f["required"] else "否"} | {f["notes"].replace("|", "/")} |')
        lines += [''] + ['- ' + rule for rule in t['rules']] + ['']
    out = ROOT / 'docs/database-design/FIELD_DICTIONARY.md'
    out.write_text('\n'.join(lines), encoding='utf-8')
    print(f'Validated {len(TABLES)} tables and {sum(len(t["fields"]) for t in TABLES)} fields; wrote review-only design files.')


if __name__ == '__main__':
    main()
