"""Generate the researched, review-only channel-order model. Never connect to live Teable."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TABLES = []


def add(key, name, grain, fields, rules=(), group='business'):
    converted = []
    for f in fields:
        item = dict(zip(['key', 'name', 'type', 'notes'], f[:4]))
        if len(f) > 4:
            item['target'] = f[4]
        converted.append(item)
    TABLES.append({'key': key, 'name': name, 'group': group, 'grain': grain,
                   'primary_key': 'immutable record ID', 'fields': converted, 'rules': list(rules)})


add('regions', '业务大区', '一个业务管理大区；目前为欧洲、东南亚、南美。', [
    ('code', '大区编码', 'text', '稳定唯一编码，不按名称变化新建'),
    ('name', '大区名称', 'text', '欧洲/东南亚/南美；采用用户的业务分组'),
    ('active', '启用', 'checkbox', '停用不删除历史引用'),
])
add('markets', '地区市场', '一个经确认的地区市场；不是客户或业务渠道。', [
    ('code', '市场编码', 'text', '稳定唯一编码'),
    ('name', '市场名称', 'text', '如波兰、巴西、越南；示例不是本次新增的真实记录'),
    ('region', '业务大区', 'linkOne', '正常归属一个业务大区；来源冲突时暂空并待核对', 'regions'),
    ('owner', '市场负责人', 'user', '业务负责人'),
    ('active', '启用', 'checkbox', '历史市场不删除'),
], ['市场的大区来自确认的业务归属，不强按地理位置或旧工作表括号标签推断。'])
add('customers', '客户档案', '一个可识别的业务客户；同一客户可以服务多个市场。', [
    ('code', '客户编码', 'text', '稳定唯一编码；名称不是唯一键'),
    ('name', '客户名称', 'text', '公司或业务客户的正式名称'),
    ('markets', '经营市场', 'linkMany', '通常一个市场；跨市场同一客户复用档案，各市场下均可查看', 'markets'),
    ('owner', '客户负责人', 'user', '负责跟进的业务人员'),
    ('state', '合作状态', 'singleSelect', '潜在/合作中/暂停/停止'),
    ('notes', '客户说明', 'longText', '公司背景等；结构化偏好另记'),
], ['客户不等同地区市场，也不凭渠道名称自动生成。',
    '同名不自动合并，跨市场确认为同一客户才复用；市场未确认时留待核对。'])
add('tags', '标签字典', '一个可复用的标签概念；客户偏好与产品标注使用同一字典。', [
    ('code', '标签编码', 'text', '稳定唯一编码，避免RGB/RGB灯效等重复概念'),
    ('name', '标签名称', 'text', '如性价比、RGB灯效、简约外观、无线连接'),
    ('group', '标签分类', 'singleSelect', '产品定位/灯效外观/连接方式/使用场景/功能体验等'),
    ('definition', '定义及标注标准', 'longText', '性价比等主观定位须说明依据和适用范围'),
    ('aliases', '别名', 'longText', '便于搜索和迁移；不另建同义标签'),
    ('applicable_to', '可用于', 'multipleSelect', '客户偏好/产品标注；不是所有偏好都可作为产品事实'),
    ('active', '启用', 'checkbox', '停用保留历史，默认不供新标注选择'),
])
add('customer_preferences', '客户偏好', '一个客户在指定市场、品类下对某标签的一条偏好版本。', [
    ('customer', '客户', 'linkOne', '偏好归属客户', 'customers'),
    ('market', '适用市场', 'linkOne', '可空表示该客户全部经营市场；有值时必须属于其经营市场', 'markets'),
    ('category_scope', '适用品类', 'singleSelect', '可空表示全品类；与SKU主品类共用受控选项'),
    ('tag', '偏好标签', 'linkOne', '只能选允许用于客户偏好的标签', 'tags'),
    ('stance', '偏好方向', 'singleSelect', '偏好/必须具备/避开'),
    ('priority', '重要程度', 'singleSelect', '高/中/低；同一客户可有多个高优先偏好'),
    ('basis', '依据', 'longText', '客户反馈/业务观察/历史订单等；未核实推测须注明'),
    ('confirmed_on', '最近核实日期', 'date', '缺失不冒充已核实'),
    ('valid_from', '生效日期', 'date', '可空表示未知'),
    ('valid_to', '失效日期', 'date', '可空表示未设结束日，不能早于生效日期'),
    ('state', '状态', 'singleSelect', '待核实/有效/已替代/已失效'),
    ('owner', '记录负责人', 'user', '负责核实和维护'),
], ['同一客户/市场范围/品类范围/标签同一时点仅一条有效偏好；变更保留历史。',
    '不把某客户偏好扩散给该地区所有客户；空白表示未知，不表示不喜欢。',
    '市场或品类特定记录与通用记录冲突时提示核对，不能隐式覆盖或自动推荐。'])
add('product_tags', '产品标签', '一个SKU与一个标签的标注关系；保存依据与核实状态。', [
    ('sku', '产品SKU', 'linkOne', '正式料号级别，不能只按相似型号推广', 'skus'),
    ('tag', '产品标签', 'linkOne', '只能选允许用于产品标注的标签', 'tags'),
    ('state', '核实状态', 'singleSelect', '待核实/已确认/已停用'),
    ('basis', '标注依据', 'longText', '规格书/实测/定位审核等；不能只凭名称猜RGB或无线'),
    ('confirmed_on', '最近核实日期', 'date', '确认后填写，不冒充产品发布日期'),
    ('owner', '维护人', 'user', '负责核实产品特点'),
], ['同SKU同标签只有一条当前有效标注；待核实不参与已确认产品筛选。',
    '主品类仍单独管理，标签可多选；接口、尺寸、装量等精确规格保留结构化字段。',
    '性价比是有依据的定位判断，不从低价格或客户偏好自动生成。'])

add('channels', '业务渠道', '一个下单业务渠道；不默认等同客户公司或市场。', [
    ('code', '渠道编码', 'text', '稳定唯一编码'), ('name', '渠道名称', 'text', '用户确认的渠道名称'),
    ('markets', '覆盖市场', 'linkMany', '已核对的业务范围，不凭市场自动生成渠道', 'markets'),
    ('customers', '服务客户', 'linkMany', '经确认的客户关系；不强制渠道等于某客户', 'customers'),
    ('owner', '业务负责人', 'user', '执行导入的账户不自动成为业务负责人'),
])
add('skus', '产品SKU', '一个正式物料号代表的可下单产品；型号系列不是交易行主键。', [
    ('part_number', '料号', 'text', '业务标识；记录ID独立且不可变'),
    ('brand', '品牌', 'singleSelect', '雷神/机械师等'),
    ('description_zh', '中文描述', 'longText', '保留规范描述'),
    ('description_en', '英文描述', 'longText', '缺失不猜测'),
    ('category', '主品类', 'singleSelect', '多标签冲突经确认后规范'),
    ('source_tags', '原品类及业务标签', 'multipleSelect', '全部保留原标记'),
    ('confirmed_tags', '已确认产品标签', 'derived', '来自产品标签表的已确认且启用标签；界面展示及筛选'),
    ('base_uom', '基础计量单位', 'singleSelect', '件/套等必须明确；来源未给出时待确认'),
    ('manufacturer', '制造商', 'text', '独立于品牌/供应商/ODM'),
    ('supplier', '供应商', 'text', '原字段保留'), ('odm', 'ODM', 'text', '原字段保留'),
    ('pid_raw', 'PID原文', 'text', '0有效；USB用途台账第二阶段处理'),
    ('variant_attributes', '已确认版本属性', 'longText', '颜色/轴体/布局/语言/套装等经确认记录，不凭名称自动拆分'),
], ['经确认的品牌+料号是候选唯一业务键；所有交易以不可变记录ID关联。'])
add('packaging', '包装规格', '一个SKU的一种包装层级和装量版本。', [
    ('sku', '产品SKU', 'linkOne', '正式规格必须关联SKU', 'skus'),
    ('level', '包装层级', 'singleSelect', '单件/内盒/外箱等'),
    ('units_per_pack', '每包装基础数量', 'decimal', '正数；未确认装量时不得作为换算依据'),
    ('version', '规格版本', 'text', '装量改变新建版本，不回写历史单据'),
    ('confirmed', '换算已确认', 'checkbox', '原自由文本箱规不能直接视为已确认换算'),
    ('source_spec', '原箱规描述', 'text', '保留原文'),
], ['包装主数据改变不得改变已确认单据上的换算快照。'])
add('barcodes', '条码分配', '一个编码的分配/核对记录；预留码允许尚无SKU或包装。', [
    ('raw_code', '条码原文', 'text', '保留前导零'),
    ('scheme', '编码体系', 'singleSelect', 'GTIN13/GTIN14/其他内部码/待确认'),
    ('normalized_gtin', '规范GTIN', 'text', '仅校验通过的GTIN规范成14位比较键；原文保留'),
    ('sku', '产品SKU', 'linkOne', '待补料号或预留时可空', 'skus'),
    ('packaging', '包装规格', 'linkOne', '包装对应关系经确认，不任取一个同SKU条码', 'packaging'),
    ('state', '分配状态', 'singleSelect', '预留/已分配/公式待计算/待核对/停用'),
    ('validation', '校验状态', 'singleSelect', '长度、校验位、对应关系分别验证'),
    ('source_row', '来源行', 'linkOne', '原始证据', 'source_rows'),
], ['同料号多码先核对包装/编码主体/历史差异，不能直接宣称是历史版本。'])
add('warehouses', '基地仓库', '一个实际发生渠道提货或退回的地点。', [
    ('code', '基地仓库编码', 'text', '唯一编码'), ('name', '基地仓库名称', 'text', '正式名称'),
    ('active', '可使用', 'checkbox', '停用不删除历史引用'),
], ['这里只记录交接地点；没有完整收发存数据时不得声称能计算可用库存。'])

add('order_batches', '渠道订单批次', '用户确认：一个业务渠道下单编号；可跨月、包含多个SKU。', [
    ('batch_number', '订单批次号', 'text', '正式渠道下单编号；不是随意的分组备注'),
    ('channel', '业务渠道', 'linkOne', '已确认订单必须明确渠道', 'channels'),
    ('default_market', '默认地区市场', 'linkOne', '录入明细时预填；实际统计按明细市场，大区沿市场查询', 'markets'),
    ('default_customer', '默认客户', 'linkOne', '录入明细时预填；未知可空，不用市场名冒充客户', 'customers'),
    ('placed_date', '渠道下单日期', 'date', '未知不补成导入日'),
    ('document_state', '订单状态', 'singleSelect', '草稿/已确认/暂停/已关闭/已取消'),
    ('source_batch_label', '原表批次文字', 'text', '缩写/日期描述/缺失字段先核对为正式编号'),
    ('owner', '负责人', 'user', '业务负责人'),
    ('source_rows', '来源行', 'linkMany', '可由多条旧行核对归并', 'source_rows'),
], ['默认唯一范围为渠道+批次号；是否全公司唯一仍待确认。',
    '月份和SKU都不是批次主键的一部分；更改排期不新建同一订单。',
    '正式渠道订单和向基地提交预测是独立过程。',
    '批次归属于渠道，客户和市场以明细归属为准，默认值变更不回写已确认明细。',
    '不能把整个渠道批次的数量重复归给每个客户或地区市场。'])
add('order_lines', '批次SKU明细', '一个批次内一个明确的SKU及交易要求行；不是一个月份一行。', [
    ('batch', '渠道订单批次', 'linkOne', '确认后必填', 'order_batches'),
    ('line_number', '批次内行号', 'integer', '与批次组合唯一；不同要求的同SKU可多行'),
    ('sku', '产品SKU', 'linkOne', '确认后必填', 'skus'),
    ('market', '地区市场', 'linkOne', '实际业务归属；未知待核对。客户/市场统计以明细为粒度', 'markets'),
    ('customer', '客户', 'linkOne', '可由主单预填并确认；未知待核对，不用渠道或市场冒充', 'customers'),
    ('description_snapshot', '订单描述快照', 'longText', '下单时名称不随主数据改变'),
    ('requirements', '客户版本及要求', 'longText', '颜色/语言/轴体等不同要求不强行合并'),
    ('line_role', '明细性质', 'singleSelect', '常规/赠品/备品/其他；数量和指标分别过滤'),
    ('ordered_qty', '原始下单数量', 'decimal', '基础单位数量；确认后按受控变更调整'),
    ('base_uom_snapshot', '基础单位快照', 'text', '明确件/套等'),
    ('current_order_qty', '当前确认数量', 'derived', '原始数量+已确认增减单'),
    ('closed_qty', '已关闭余量', 'derived', '关闭-重新开放，不能伪装成已提货'),
    ('gross_picked_qty', '累计实际提货', 'derived', '期初+已确认未冲销的实际提货分配'),
    ('returned_qty', '实际退回基地', 'derived', '真实退回，区别于录入错误的冲销'),
    ('net_picked_qty', '净提货量', 'derived', '累计实际提货-实际退回'),
    ('fulfillment_credit_qty', '已履约数量', 'derived', '实际提货-需要补提的退回量，含已确认期初'),
    ('to_pick_qty', '待提货', 'derived', '当前确认数量-关闭余量-已履约数量；期初未知时为空'),
    ('pickup_state', '提货状态', 'derived', '未提/部分提/提货完成/超提异常；与订单状态独立'),
    ('source_rows', '来源行', 'linkMany', '多个原排期行归并后仍可追溯', 'source_rows'),
], ['同一SKU不同交期通常属于同一明细的多个计划；不能靠源行号生成新业务身份。',
    '是否合并相同SKU须先确认客户、市场、交易要求、单位和源数量不是重复快照。',
    '已知客户时明细市场须属于客户经营市场；同SKU服务不同客户或市场时分行记录。'])
add('line_plans', '分月计划', '某条SKU明细在一个计划节点、期间/日期上的数量版本。', [
    ('line', '批次SKU明细', 'linkOne', '计划始终从属于正式明细', 'order_lines'),
    ('plan_kind', '计划节点', 'singleSelect', '基地预测/预计提货/预计客户交付/原计划节点待核对'),
    ('month', '计划年月', 'text', 'YYYY-MM；跨年可辨，不能只填10月'),
    ('exact_date', '预计具体日期', 'date', '可空；只有月份时不伪造某一天'),
    ('planned_qty', '计划数量', 'decimal', '基础单位量，不是新增订单数量'),
    ('forecast_state', '基地预测提交状态', 'singleSelect', '未提交/部分提交/已提交/不适用'),
    ('submitted_date', '基地预测提交日期', 'date', '原J列对应节点先核对'),
    ('state', '版本状态', 'singleSelect', '有效/已替代/已取消'),
    ('supersedes', '替代旧计划', 'linkOne', '保留改期历史，不能让新旧版本同时计入', 'line_plans'),
    ('change_reason', '改期说明', 'longText', '记录原因'),
], ['待提交基地预测不等同渠道尚未下单。',
    '不同计划节点单独统计，不把预测+提货计划+客户交付计划相加。',
    '提货时间不固定时允许没有具体日期；实际提货不强制关联一个预定日期。'])

add('handover_docs', '提货退回单', '一次基地与渠道的交接凭据；一张单可含多个SKU和多个订单批次。', [
    ('document_number', '交接单号', 'text', '稳定唯一编号'),
    ('type', '单据类型', 'singleSelect', '实际提货/实际退回/错误冲销'),
    ('channel', '业务渠道', 'linkOne', '同张单据为同一渠道', 'channels'),
    ('warehouse', '基地仓库', 'linkOne', '同张单据为同一交接地点', 'warehouses'),
    ('occurred_date', '实际交接日期', 'date', '发生时登记，不要求预先排定'),
    ('state', '单据状态', 'singleSelect', '草稿/已确认/已冲销'),
    ('posted_at', '确认时间', 'dateTime', '实际业务日期与系统确认时间分开'),
    ('evidence', '交接凭据', 'attachment', '提货单/退回单等'),
    ('reverses', '冲销原单', 'linkOne', '错误冲销必须指向原单；不是实物退货', 'handover_docs'),
], ['草稿不计数；确认后不原地改SKU、数量或分配，错误通过受控冲销/更正单处理。'])
add('handover_lines', '交接单SKU行', '一次实际交接中的一种SKU及包装；不直接绑定某一个订单。', [
    ('document', '交接单', 'linkOne', '所属单据', 'handover_docs'),
    ('line_number', '凭据行号', 'integer', '单据内唯一'),
    ('sku', '产品SKU', 'linkOne', '实际交接SKU', 'skus'),
    ('packaging', '包装规格', 'linkOne', '按箱输入时必须已确认换算', 'packaging'),
    ('quantity', '交接数量', 'decimal', '按实际输入单位保存的正数'),
    ('uom_snapshot', '输入单位快照', 'text', '箱/件/套等'),
    ('conversion_snapshot', '基础单位换算快照', 'decimal', '只取当时已确认的正换算，不随主数据改变'),
    ('base_quantity', '基础单位数量', 'derived', '数量×换算；需要整数件时拒绝分数'),
], ['一张物理凭据上的总数量只记一次，再由履约分配拆给多个订单，避免重复计提。'])
add('allocations', '履约分配', '一条交接行分配给某条订单明细的基础数量。', [
    ('handover_line', '交接单SKU行', 'linkOne', '实际凭据来源', 'handover_lines'),
    ('order_line', '批次SKU明细', 'linkOne', '履约归属', 'order_lines'),
    ('quantity', '分配基础数量', 'decimal', '正数；同张交接行的分配合计必须等于实交数量'),
    ('plan', '对应计划', 'linkOne', '可空；不固定时间的提货也可直接落到订单明细', 'line_plans'),
    ('return_against', '原提货分配', 'linkOne', '实际退回必须可追溯原已提分配', 'allocations'),
    ('return_resolution', '退回后处理', 'singleSelect', '需要补提/不再补提/待确认；不从退回动作自动推断'),
], ['SKU、基础单位、渠道必须与所关联单据/订单一致。',
    '同一SKU的一次提货可分配到多个批次；只计物理数量一次。',
    '退回累计不得超过该原分配可退量；需要补提的退回才重新打开履约需求。'])
add('line_actions', '订单数量变更', '对某条订单明细的一次有依据的增减、关闭或重开余量。', [
    ('line', '批次SKU明细', 'linkOne', '受影响明细', 'order_lines'),
    ('action', '动作', 'singleSelect', '增加下单量/减少下单量/关闭余量/重开余量'),
    ('quantity', '基础数量', 'decimal', '正数，方向由动作确定'),
    ('reason', '原因', 'longText', '必须有渠道确认或业务依据'),
    ('state', '确认状态', 'singleSelect', '草稿/已确认/已冲销'),
    ('evidence', '依据', 'attachment', '经确认的改单/余量关闭依据'),
], ['关闭余量不是提货；已关闭部分不进入待提货。', '不能直接把当前数量减到已履约数量和已关闭余量之下。'])
add('followups', '跟进记录', '一条共享或单独的业务文字说明，不能代替数量凭据。', [
    ('batch', '渠道订单批次', 'linkOne', '批次级说明可只挂批次', 'order_batches'),
    ('lines', '相关SKU明细', 'linkMany', '需要时挂多条明细', 'order_lines'),
    ('body', '内容', 'longText', '保留历史共享备注'),
    ('business_date', '业务记录日期', 'date', '未知时空，不用导入日充当历史日期'),
    ('source_row', '来源行', 'linkOne', '精确原始出处', 'source_rows'),
])

add('opening_balances', '期初履约确认', '一条经审核的历史余额说明；不是编造的历史提货单。', [
    ('line', '批次SKU明细', 'linkOne', '每明细每有效截点只有一套余额', 'order_lines'),
    ('cutover_date', '历史截点', 'date', '明确到日；边界后新单据才计增量'),
    ('gross_picked_qty', '累计提货期初', 'decimal', '未知与0分开'),
    ('returned_qty', '累计退回期初', 'decimal', '必须确认，不从历史散文编造'),
    ('fulfillment_credit', '已履约期初', 'decimal', '退回处理差异须在说明中体现'),
    ('closed_qty', '关闭余量期初', 'decimal', '已完结不能自动推导全部提货或全部关闭'),
    ('confirmed', '已审核', 'checkbox', '未经审核权威余额不显示为零'),
    ('basis', '依据及假设', 'longText', '保留确认人和证据'),
], group='migration')
add('import_batches', '导入快照', '一次原文件快照；与业务订单批次分开。', [
    ('filename', '文件名', 'text', '原件保存在受限存储'),
    ('sha256', '文件哈希', 'text', '同快照重跑幂等'),
    ('mapping_version', '映射版本', 'text', '可重放'),
    ('imported_at', '导入时间', 'dateTime', '不作为业务日期'),
], group='migration')
add('source_rows', '原始台账行', '原表一行观察记录；先核对其批次、SKU和计划粒度。', [
    ('snapshot', '导入快照', 'linkOne', '对应文件', 'import_batches'),
    ('sheet', '工作表', 'text', '原标签'), ('row', '行号', 'integer', '来源定位，非业务ID'),
    ('business_values', '白名单原值', 'longText', '排除凭据、隐藏页和非业务字段'),
    ('classification', '归属核对', 'singleSelect', '正式订单/分月计划/意向需求/重复快照/待确认'),
    ('matched_batch', '对应订单批次', 'linkOne', '渠道编号确认后关联', 'order_batches'),
    ('matched_line', '对应SKU明细', 'linkOne', '不能用源行号代替稳定明细ID', 'order_lines'),
    ('matched_plan', '对应计划', 'linkOne', '需要时映射计划节点', 'line_plans'),
], ['新文件SHA改变时不能直接追加另一套订单；先核对稳定业务身份。'], group='migration')
add('review_issues', '数据核对', '一个数据冲突、缺失或归并问题。', [
    ('source', '来源行', 'linkOne', '原始证据', 'source_rows'),
    ('kind', '问题类型', 'singleSelect', '条码/料号/批次/品牌/计划节点/状态数量/市场归属/客户身份/标签偏好等'),
    ('state', '处理状态', 'singleSelect', '未处理/处理中/已解决/暂缓'),
    ('decision', '处理结论', 'longText', '不覆盖原始证据'),
], group='migration')

MODEL = {
    'version': 'research-draft-2026-10-20-v2.1', 'status': 'review_only', 'supersedes': 'draft-2026-10-20-v1',
    'execution': 'Logical model and required business controls; not a Teable API request or live migration.',
    'confirmed_rules': [
        '订单批次是业务渠道下单编号，一个批次可以有多个SKU，跨越多个月份，实际提货时间不固定。',
        '已提货表示渠道业务从基地/仓库实际提走，不能当成国际发运或客户签收。',
        '市场按欧洲、东南亚、南美三个业务大区管理，大区下分地区市场，市场下有不同客户。',
        '需要客户偏好记录（如性价比、RGB灯效）及用于产品归类的标签。',
    ],
    'modeling_decisions': ['向基地提交预测的状态与渠道是否已经下单分开；具体业务节点仍待核对。'],
    'references': [
        {'title': 'ERPNext Sales Order', 'url': 'https://docs.frappe.io/erpnext/sales-order', 'adopt': 'Order header/lines; commitment separate from fulfillment'},
        {'title': 'ERPNext Delivery Note', 'url': 'https://docs.frappe.io/erpnext/delivery-note', 'adopt': 'Document and item rows; partial fulfillment'},
        {'title': 'ERPNext Close Sales Order', 'url': 'https://docs.frappe.io/erpnext/close-sales-order', 'adopt': 'Short close separate from completed pickup and cancellation'},
        {'title': 'ERPNext Item Variants', 'url': 'https://docs.frappe.io/erpnext/item-variants', 'adopt': 'Transactional SKU distinct from optional model/template'},
        {'title': 'Odoo Routes', 'url': 'https://www.odoo.com/documentation/19.0/applications/inventory_and_mrp/inventory/shipping_receiving/daily_operations/use_routes.html', 'adopt': 'Physical milestones remain separate; warehouse picking is not automatically channel handover'},
        {'title': 'GS1 Pack/case quantity', 'url': 'https://www.gs1.org/1/gtinrules/en/rule/270/packcase-quantity', 'adopt': 'Packaging levels and changed pack quantities require explicit GTIN handling'},
    ],
    'tables': TABLES,
    'region_seeds': [{'code': 'EU', 'name': '欧洲'}, {'code': 'SEA', 'name': '东南亚'}, {'code': 'SA', 'name': '南美'}],
    'tag_examples': [
        {'code': 'VALUE_FOR_MONEY', 'name': '性价比', 'group': '产品定位', 'applicable_to': ['客户偏好', '产品标注'], 'definition': '定位判断；须记录比较范围和业务依据，不能等同低价。'},
        {'code': 'RGB_LIGHTING', 'name': 'RGB灯效', 'group': '灯效外观', 'applicable_to': ['客户偏好', '产品标注'], 'definition': '客户可表达喜好或避开；产品须经规格/实测核实。'},
        {'code': 'MINIMAL_DESIGN', 'name': '简约外观', 'group': '灯效外观', 'applicable_to': ['客户偏好', '产品标注'], 'definition': '候选标签，标注标准需经审核。'},
        {'code': 'WIRELESS', 'name': '无线连接', 'group': '连接方式', 'applicable_to': ['客户偏好', '产品标注'], 'definition': '产品有可用无线连接；具体协议仍由规格字段表达。'},
    ],
    'example_policy': 'Tag examples are candidate vocabulary, not assertions about any real customer or SKU. Region seeds reflect confirmed business groups; no live records are created.',
    'classification_requirements': [
        '地区市场归属业务大区；未知或源分组冲突先核对，尤其不能把香港强塞入东南亚。',
        '客户有稳定身份，可覆盖多个市场；客户偏好可限定市场及品类，不能从地域自动继承。',
        '订单市场/客户/渠道需要一致性校验；不得凭空为历史订单创建客户。',
        '标签字典统一概念；客户偏好方向/权重与产品事实分别保存，未知不等同不具备。',
        'Product/customer tag scope, current preference uniqueness, active dates and evidence state must be validated during implementation; this contract does not enforce live writes.',
        '标签用于查找候选；实际报价、规格、库存与交付需另行核实，不提供未经验证的自动匹配分数。',
    ],
    'posting_requirements': [
        'Posting requires one atomic/idempotent controlled write path; a Teable rollup alone is not an ERP posting engine.',
        'Check document state, matching channel/SKU/UOM, exact allocation totals and remaining order allowance at posting time.',
        'Same document cannot post twice; concurrent posts cannot both spend the same remaining allowance.',
        'Posted values are not directly editable. Authorized reversal is linked and has opposite ledger effects exactly once.',
        'Validate actual Community edition permissions and transaction behavior before enabling multi-user posting.',
        'No inventory availability, revenue, ownership-transfer or financial-ledger claims from this model alone.',
    ],
    'optional_later_modules': ['型号系列与变体属性规范', '海外发运与客户签收单据', '客户多地址及联系人扩展', 'USB VID/PID用途台账', '价格币种、开票和收款对接', '独立意向需求/预测转单（如实际存在该流程）'],
    'decisions_pending': ['批次号是全公司唯一还是仅渠道内唯一', '原K列计划交付节点', '实际退回后补提还是不再补提', '数量单位和箱规换算', '历史期初数量和关闭余量', '渠道与客户公司的对应关系', '各地区市场的大区归属及香港等旧分组冲突', '真实客户名单与经营市场', '标签定义、产品核实依据及客户偏好初始资料'],
}


def validate(model):
    keys = {t['key'] for t in model['tables']}
    assert len(keys) == len(model['tables']), 'duplicate table keys'
    for t in model['tables']:
        assert len({f['key'] for f in t['fields']}) == len(t['fields']), t['key']
        for f in t['fields']:
            if 'target' in f:
                assert f['target'] in keys, (t['key'], f['target'])
    assert {'order_batches', 'order_lines', 'line_plans', 'handover_docs', 'handover_lines', 'allocations'} <= keys
    assert {'regions', 'markets', 'customers', 'tags', 'customer_preferences', 'product_tags'} <= keys
    assert len({r['code'] for r in model['region_seeds']}) == len(model['region_seeds'])
    assert len({t['code'] for t in model['tag_examples']}) == len(model['tag_examples'])


def main():
    validate(MODEL)
    (ROOT / 'schema/teable-model-v2.json').write_text(json.dumps(MODEL, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    out = ['# V2.1字段字典', '', '这是研究修订草案，不是已建成的Teable表或已实现的约束。', '']
    for t in TABLES:
        out += [f'## {t["name"]}', '', t['grain'], '', '| 字段 | 逻辑类型 | 含义 |', '| --- | --- | --- |']
        out += [f'| {f["name"]} | {f["type"]}' + (f' → {f["target"]}' if 'target' in f else '') + f' | {f["notes"]} |' for f in t['fields']]
        out += [''] + ['- ' + x for x in t['rules']] + ['']
    (ROOT / 'docs/database-design/V2_FIELD_DICTIONARY.md').write_text('\n'.join(out), encoding='utf-8')
    print(f'V2: {len(TABLES)} tables, {sum(len(t["fields"]) for t in TABLES)} fields; logical references valid.')


if __name__ == '__main__':
    main()
