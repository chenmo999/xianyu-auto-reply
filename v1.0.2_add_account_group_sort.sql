-- v1.0.2.1 账号分组 + 账号排序
-- 安全可重复执行；不会删除数据。

-- 1) 账号表补字段：分组 / 排序
SET @db_name := DATABASE();

SET @sql := (
  SELECT IF(
    COUNT(*) = 0,
    'ALTER TABLE xy_accounts ADD COLUMN category VARCHAR(32) NOT NULL DEFAULT ''默认'' COMMENT ''账号分类/分组''',
    'SELECT ''category already exists'''
  )
  FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = @db_name
    AND TABLE_NAME = 'xy_accounts'
    AND COLUMN_NAME = 'category'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @sql := (
  SELECT IF(
    COUNT(*) = 0,
    'ALTER TABLE xy_accounts ADD COLUMN sort_order INT NOT NULL DEFAULT 0 COMMENT ''账号排序''',
    'SELECT ''sort_order already exists'''
  )
  FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = @db_name
    AND TABLE_NAME = 'xy_accounts'
    AND COLUMN_NAME = 'sort_order'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- 2) 给旧数据补默认分组和排序
UPDATE xy_accounts
SET category = '默认'
WHERE category IS NULL OR TRIM(category) = '';

SET @rownum := 0;
UPDATE xy_accounts
SET sort_order = (@rownum := @rownum + 1)
WHERE sort_order IS NULL OR sort_order = 0
ORDER BY category ASC, id ASC;

-- 3) 建立持久化分组表：新增空分组也会保存，不依赖账号表是否已有账号
CREATE TABLE IF NOT EXISTS xy_account_groups (
  id BIGINT NOT NULL AUTO_INCREMENT COMMENT '分组ID',
  owner_id BIGINT NOT NULL COMMENT '所属用户ID',
  name VARCHAR(32) NOT NULL COMMENT '分组名称',
  sort_order INT NOT NULL DEFAULT 0 COMMENT '分组排序',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_account_group_owner_name (owner_id, name),
  KEY idx_account_group_owner_sort (owner_id, sort_order)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='账号分组表';

-- 4) 把账号表里已有分组同步到分组表
INSERT IGNORE INTO xy_account_groups (owner_id, name, sort_order)
SELECT owner_id, category, 0
FROM xy_accounts
WHERE category IS NOT NULL AND TRIM(category) <> ''
GROUP BY owner_id, category;

-- 5) 每个已有账号用户补默认分组
INSERT IGNORE INTO xy_account_groups (owner_id, name, sort_order)
SELECT u.owner_id, d.name, d.sort_order
FROM (SELECT DISTINCT owner_id FROM xy_accounts) AS u
CROSS JOIN (
  SELECT '默认' AS name, 0 AS sort_order
  UNION ALL SELECT '工作', 1
  UNION ALL SELECT '私人', 2
  UNION ALL SELECT '测试', 3
) AS d;

-- 6) 索引：已存在则跳过
SET @idx_exists := (
  SELECT COUNT(*)
  FROM information_schema.STATISTICS
  WHERE TABLE_SCHEMA = @db_name
    AND TABLE_NAME = 'xy_accounts'
    AND INDEX_NAME = 'idx_account_category_sort'
);
SET @sql := IF(
  @idx_exists = 0,
  'CREATE INDEX idx_account_category_sort ON xy_accounts(category, sort_order)',
  'SELECT ''idx_account_category_sort already exists'''
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
