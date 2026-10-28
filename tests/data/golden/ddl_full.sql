CREATE TABLE `ledger_meta` (
  `key` VARCHAR(64) NOT NULL,
  `value` MEDIUMTEXT,
  CONSTRAINT `pk_ledger_meta` PRIMARY KEY (`key`)
);

CREATE TABLE `entity` (
  `id` VARCHAR(64) NOT NULL,
  `kind` VARCHAR(64) NOT NULL,
  `name` VARCHAR(255),
  `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `notes` MEDIUMTEXT,
  CONSTRAINT `pk_entity` PRIMARY KEY (`id`),
  CONSTRAINT `uq_entity_id_kind` UNIQUE (`id`, `kind`)
);

CREATE TABLE `stock` (
  `id` VARCHAR(64) NOT NULL,
  `kind` VARCHAR(64) NOT NULL,
  `entity_id` VARCHAR(64) NOT NULL,
  `parent_stock_id` VARCHAR(64),
  `source_aliquot_id` VARCHAR(64),
  `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `notes` MEDIUMTEXT,
  CONSTRAINT `pk_stock` PRIMARY KEY (`id`),
  CONSTRAINT `uq_stock_id_kind` UNIQUE (`id`, `kind`)
);

CREATE TABLE `box` (
  `id` VARCHAR(64) NOT NULL,
  `name` VARCHAR(255) NOT NULL,
  `kind` VARCHAR(64) NOT NULL,
  `parent_box_id` VARCHAR(64),
  `parent_slot` VARCHAR(64),
  `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `notes` MEDIUMTEXT,
  CONSTRAINT `pk_box` PRIMARY KEY (`id`),
  CONSTRAINT `uq_box_name` UNIQUE (`name`),
  CONSTRAINT `uq_box_id_kind` UNIQUE (`id`, `kind`)
);

CREATE TABLE `slot` (
  `box_id` VARCHAR(64) NOT NULL,
  `label` VARCHAR(64) NOT NULL,
  `row_label` VARCHAR(64),
  `col_label` VARCHAR(64),
  `ordinal` INT NOT NULL,
  CONSTRAINT `pk_slot` PRIMARY KEY (`box_id`, `label`)
);

CREATE TABLE `aliquot` (
  `id` VARCHAR(64) NOT NULL,
  `stock_id` VARCHAR(64) NOT NULL,
  `seq` INT NOT NULL,
  `box_id` VARCHAR(64),
  `slot` VARCHAR(64),
  `status` VARCHAR(32) NOT NULL,
  `quantity` DECIMAL(18,6),
  `unit` VARCHAR(32),
  `label` VARCHAR(255),
  `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `notes` MEDIUMTEXT,
  CONSTRAINT `pk_aliquot` PRIMARY KEY (`id`),
  CONSTRAINT `uq_aliquot_stock_id_seq` UNIQUE (`stock_id`, `seq`),
  CONSTRAINT `ck_aliquot_status` CHECK (`status` IN ('pending', 'stored', 'missing', 'consumed'))
);

CREATE TABLE `aliquot_event` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `aliquot_id` VARCHAR(64) NOT NULL,
  `event` ENUM('create', 'use', 'move', 'status', 'edit') NOT NULL,
  `amount` DECIMAL(18,6),
  `unit` VARCHAR(32),
  `from_box_id` VARCHAR(64),
  `from_slot` VARCHAR(64),
  `to_box_id` VARCHAR(64),
  `to_slot` VARCHAR(64),
  `old_status` VARCHAR(32),
  `new_status` VARCHAR(32),
  `actor` VARCHAR(255),
  `purpose` MEDIUMTEXT,
  `note` MEDIUMTEXT,
  `at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT `pk_aliquot_event` PRIMARY KEY (`id`)
);

CREATE TABLE `alias` (
  `object_type` VARCHAR(16) NOT NULL,
  `object_id` VARCHAR(64) NOT NULL,
  `alias` VARCHAR(255) NOT NULL,
  CONSTRAINT `pk_alias` PRIMARY KEY (`object_type`, `alias`)
);

ALTER TABLE `stock` ADD CONSTRAINT `fk_stock_entity_id` FOREIGN KEY (`entity_id`) REFERENCES `entity` (`id`);

ALTER TABLE `stock` ADD CONSTRAINT `fk_stock_parent_stock_id` FOREIGN KEY (`parent_stock_id`) REFERENCES `stock` (`id`);

ALTER TABLE `stock` ADD CONSTRAINT `fk_stock_source_aliquot_id` FOREIGN KEY (`source_aliquot_id`) REFERENCES `aliquot` (`id`);

ALTER TABLE `box` ADD CONSTRAINT `fk_box_parent_box_id` FOREIGN KEY (`parent_box_id`) REFERENCES `box` (`id`);

ALTER TABLE `slot` ADD CONSTRAINT `fk_slot_box_id` FOREIGN KEY (`box_id`) REFERENCES `box` (`id`);

ALTER TABLE `aliquot` ADD CONSTRAINT `fk_aliquot_stock_id` FOREIGN KEY (`stock_id`) REFERENCES `stock` (`id`);

ALTER TABLE `aliquot` ADD CONSTRAINT `fk_aliquot_box_id_slot` FOREIGN KEY (`box_id`, `slot`) REFERENCES `slot` (`box_id`, `label`);

ALTER TABLE `aliquot_event` ADD CONSTRAINT `fk_aliquot_event_aliquot_id` FOREIGN KEY (`aliquot_id`) REFERENCES `aliquot` (`id`);

CREATE TABLE `entity_everything` (
  `id` VARCHAR(64) NOT NULL,
  `kind` VARCHAR(64) NOT NULL DEFAULT 'everything',
  `name` VARCHAR(255) NOT NULL,
  `description` MEDIUMTEXT,
  `code` VARCHAR(12),
  `blurb` MEDIUMTEXT,
  `count` BIGINT DEFAULT 0,
  `ratio` DECIMAL(8,3),
  `active` TINYINT(1) DEFAULT TRUE,
  `born` DATE DEFAULT '2024-01-31',
  `seen_at` DATETIME,
  `color` VARCHAR(255) DEFAULT 'red',
  `seq` MEDIUMTEXT,
  `seq_sha256` CHAR(64) GENERATED ALWAYS AS (SHA2(`seq`, 256)) STORED,
  `parent` VARCHAR(64),
  `display` VARCHAR(255) GENERATED ALWAYS AS (CONCAT(name, '-', code)) STORED,
  `legacy` VARCHAR(255),
  CONSTRAINT `pk_entity_everything` PRIMARY KEY (`id`),
  CONSTRAINT `ck_entity_everything_kind` CHECK (`kind` = 'everything'),
  CONSTRAINT `uq_entity_everything_name` UNIQUE (`name`),
  CONSTRAINT `ck_entity_everything_color` CHECK (`color` IN ('red', 'green', 'blue')),
  CONSTRAINT `uq_entity_everything_seq` UNIQUE (`seq_sha256`)
);

CREATE TABLE `entity_item` (
  `id` VARCHAR(64) NOT NULL,
  `kind` VARCHAR(64) NOT NULL DEFAULT 'item',
  `name` VARCHAR(255) NOT NULL,
  `description` MEDIUMTEXT,
  `category` VARCHAR(32),
  CONSTRAINT `pk_entity_item` PRIMARY KEY (`id`),
  CONSTRAINT `ck_entity_item_kind` CHECK (`kind` = 'item'),
  CONSTRAINT `uq_entity_item_name` UNIQUE (`name`)
);

CREATE TABLE `entity_plasmid` (
  `id` VARCHAR(64) NOT NULL,
  `kind` VARCHAR(64) NOT NULL DEFAULT 'plasmid',
  `name` VARCHAR(255) NOT NULL,
  `description` MEDIUMTEXT,
  `sequence` MEDIUMTEXT,
  `sequence_sha256` CHAR(64) GENERATED ALWAYS AS (SHA2(`sequence`, 256)) STORED,
  `length_bp` BIGINT GENERATED ALWAYS AS (CHAR_LENGTH(sequence)) STORED,
  `backbone` VARCHAR(255) DEFAULT 'puc',
  CONSTRAINT `pk_entity_plasmid` PRIMARY KEY (`id`),
  CONSTRAINT `ck_entity_plasmid_kind` CHECK (`kind` = 'plasmid'),
  CONSTRAINT `uq_entity_plasmid_name` UNIQUE (`name`),
  CONSTRAINT `uq_entity_plasmid_sequence` UNIQUE (`sequence_sha256`),
  CONSTRAINT `ck_entity_plasmid_backbone` CHECK (`backbone` IN ('puc', 'pbr', 'custom'))
);

CREATE TABLE `stock_bacterial` (
  `id` VARCHAR(64) NOT NULL,
  `kind` VARCHAR(64) NOT NULL DEFAULT 'bacterial',
  `measured_at` DATETIME,
  `amount` DECIMAL(10,2) DEFAULT 0,
  `strain` VARCHAR(255) NOT NULL,
  `medium` VARCHAR(255),
  CONSTRAINT `pk_stock_bacterial` PRIMARY KEY (`id`),
  CONSTRAINT `ck_stock_bacterial_kind` CHECK (`kind` = 'bacterial'),
  CONSTRAINT `ck_stock_bacterial_medium` CHECK (`medium` IN ('lb', 'tb', 'soc'))
);

CREATE TABLE `stock_lot` (
  `id` VARCHAR(64) NOT NULL,
  `kind` VARCHAR(64) NOT NULL DEFAULT 'lot',
  `supplier` VARCHAR(255) DEFAULT 'unknown',
  `received_on` DATE,
  CONSTRAINT `pk_stock_lot` PRIMARY KEY (`id`),
  CONSTRAINT `ck_stock_lot_kind` CHECK (`kind` = 'lot')
);

CREATE TABLE `stock_prep` (
  `id` VARCHAR(64) NOT NULL,
  `kind` VARCHAR(64) NOT NULL DEFAULT 'prep',
  `batch` VARCHAR(255),
  CONSTRAINT `pk_stock_prep` PRIMARY KEY (`id`),
  CONSTRAINT `ck_stock_prep_kind` CHECK (`kind` = 'prep')
);

CREATE TABLE `box_grid96` (
  `id` VARCHAR(64) NOT NULL,
  `kind` VARCHAR(64) NOT NULL DEFAULT 'grid96',
  CONSTRAINT `pk_box_grid96` PRIMARY KEY (`id`),
  CONSTRAINT `ck_box_grid96_kind` CHECK (`kind` = 'grid96')
);

CREATE TABLE `box_grid9x9` (
  `id` VARCHAR(64) NOT NULL,
  `kind` VARCHAR(64) NOT NULL DEFAULT 'grid9x9',
  CONSTRAINT `pk_box_grid9x9` PRIMARY KEY (`id`),
  CONSTRAINT `ck_box_grid9x9_kind` CHECK (`kind` = 'grid9x9')
);

CREATE TABLE `box_namedbox` (
  `id` VARCHAR(64) NOT NULL,
  `kind` VARCHAR(64) NOT NULL DEFAULT 'namedbox',
  CONSTRAINT `pk_box_namedbox` PRIMARY KEY (`id`),
  CONSTRAINT `ck_box_namedbox_kind` CHECK (`kind` = 'namedbox')
);

CREATE TABLE `box_rack` (
  `id` VARCHAR(64) NOT NULL,
  `kind` VARCHAR(64) NOT NULL DEFAULT 'rack',
  CONSTRAINT `pk_box_rack` PRIMARY KEY (`id`),
  CONSTRAINT `ck_box_rack_kind` CHECK (`kind` = 'rack')
);

CREATE TABLE `box_shelf` (
  `id` VARCHAR(64) NOT NULL,
  `kind` VARCHAR(64) NOT NULL DEFAULT 'shelf',
  CONSTRAINT `pk_box_shelf` PRIMARY KEY (`id`),
  CONSTRAINT `ck_box_shelf_kind` CHECK (`kind` = 'shelf')
);

CREATE TABLE `box_slotlessbox` (
  `id` VARCHAR(64) NOT NULL,
  `kind` VARCHAR(64) NOT NULL DEFAULT 'slotlessbox',
  CONSTRAINT `pk_box_slotlessbox` PRIMARY KEY (`id`),
  CONSTRAINT `ck_box_slotlessbox_kind` CHECK (`kind` = 'slotlessbox')
);

ALTER TABLE `entity_everything` ADD CONSTRAINT `fk_entity_everything_id_kind` FOREIGN KEY (`id`, `kind`) REFERENCES `entity` (`id`, `kind`);

ALTER TABLE `entity_everything` ADD CONSTRAINT `fk_entity_everything_parent` FOREIGN KEY (`parent`) REFERENCES `entity_everything` (`id`);

ALTER TABLE `entity_item` ADD CONSTRAINT `fk_entity_item_id_kind` FOREIGN KEY (`id`, `kind`) REFERENCES `entity` (`id`, `kind`);

ALTER TABLE `entity_plasmid` ADD CONSTRAINT `fk_entity_plasmid_id_kind` FOREIGN KEY (`id`, `kind`) REFERENCES `entity` (`id`, `kind`);

ALTER TABLE `stock_bacterial` ADD CONSTRAINT `fk_stock_bacterial_id_kind` FOREIGN KEY (`id`, `kind`) REFERENCES `stock` (`id`, `kind`);

ALTER TABLE `stock_lot` ADD CONSTRAINT `fk_stock_lot_id_kind` FOREIGN KEY (`id`, `kind`) REFERENCES `stock` (`id`, `kind`);

ALTER TABLE `stock_prep` ADD CONSTRAINT `fk_stock_prep_id_kind` FOREIGN KEY (`id`, `kind`) REFERENCES `stock` (`id`, `kind`);

ALTER TABLE `box_grid96` ADD CONSTRAINT `fk_box_grid96_id_kind` FOREIGN KEY (`id`, `kind`) REFERENCES `box` (`id`, `kind`);

ALTER TABLE `box_grid9x9` ADD CONSTRAINT `fk_box_grid9x9_id_kind` FOREIGN KEY (`id`, `kind`) REFERENCES `box` (`id`, `kind`);

ALTER TABLE `box_namedbox` ADD CONSTRAINT `fk_box_namedbox_id_kind` FOREIGN KEY (`id`, `kind`) REFERENCES `box` (`id`, `kind`);

ALTER TABLE `box_rack` ADD CONSTRAINT `fk_box_rack_id_kind` FOREIGN KEY (`id`, `kind`) REFERENCES `box` (`id`, `kind`);

ALTER TABLE `box_shelf` ADD CONSTRAINT `fk_box_shelf_id_kind` FOREIGN KEY (`id`, `kind`) REFERENCES `box` (`id`, `kind`);

ALTER TABLE `box_slotlessbox` ADD CONSTRAINT `fk_box_slotlessbox_id_kind` FOREIGN KEY (`id`, `kind`) REFERENCES `box` (`id`, `kind`);

CREATE VIEW `v_box_slots` AS
SELECT
  b.`id` AS `box_id`,
  b.`name` AS `box_name`,
  b.`kind` AS `box_kind`,
  s.`label` AS `slot`,
  s.`row_label` AS `row_label`,
  s.`col_label` AS `col_label`,
  s.`ordinal` AS `ordinal`,
  CASE
    WHEN a.`id` IS NOT NULL THEN 'aliquot'
    WHEN c.`id` IS NOT NULL THEN 'box'
    ELSE NULL
  END AS `occupant_type`,
  COALESCE(a.`id`, c.`id`) AS `occupant_id`,
  a.`id` AS `aliquot_id`,
  a.`stock_id` AS `stock_id`,
  st.`entity_id` AS `entity_id`,
  e.`kind` AS `entity_kind`,
  a.`status` AS `status`,
  c.`id` AS `child_box_id`,
  c.`name` AS `child_box_name`,
  CASE
    WHEN a.`id` IS NOT NULL THEN (SELECT GROUP_CONCAT(`alias` ORDER BY `alias` SEPARATOR ';') FROM `alias` WHERE `object_type` = 'aliquot' AND `object_id` = a.`id`)
    WHEN c.`id` IS NOT NULL THEN (SELECT GROUP_CONCAT(`alias` ORDER BY `alias` SEPARATOR ';') FROM `alias` WHERE `object_type` = 'box' AND `object_id` = c.`id`)
    ELSE NULL
  END AS `occupant_alias_list`
FROM `slot` s
JOIN `box` b ON b.`id` = s.`box_id`
LEFT JOIN `aliquot` a ON a.`box_id` = s.`box_id` AND a.`slot` = s.`label`
LEFT JOIN `box` c ON c.`parent_box_id` = s.`box_id` AND c.`parent_slot` = s.`label`
LEFT JOIN `stock` st ON st.`id` = a.`stock_id`
LEFT JOIN `entity` e ON e.`id` = st.`entity_id`;

CREATE VIEW `v_aliquot_location` AS
WITH RECURSIVE `box_path` AS (
  SELECT
    b.`id` AS `id`,
    b.`name` AS `name`,
    b.`parent_box_id` AS `parent_box_id`,
    CAST(b.`name` AS CHAR(1024)) AS `box_path`,
    0 AS `depth`
  FROM `box` b
  WHERE b.`parent_box_id` IS NULL
  UNION ALL
  SELECT
    b.`id` AS `id`,
    b.`name` AS `name`,
    b.`parent_box_id` AS `parent_box_id`,
    CONCAT(p.`box_path`, '/', b.`name`) AS `box_path`,
    p.`depth` + 1 AS `depth`
  FROM `box` b
  JOIN `box_path` p ON b.`parent_box_id` = p.`id`
)
SELECT
  a.`id` AS `aliquot_id`,
  a.`stock_id` AS `stock_id`,
  a.`seq` AS `seq`,
  a.`box_id` AS `box_id`,
  a.`slot` AS `slot`,
  a.`status` AS `status`,
  a.`quantity` AS `quantity`,
  a.`unit` AS `unit`,
  a.`label` AS `label`,
  p.`box_path` AS `box_path`,
  CASE WHEN p.`box_path` IS NULL THEN NULL
       ELSE CONCAT(p.`box_path`, ':', a.`slot`)
  END AS `location`,
  (SELECT GROUP_CONCAT(`alias` ORDER BY `alias` SEPARATOR ';') FROM `alias` WHERE `object_type` = 'aliquot' AND `object_id` = a.`id`) AS `alias_list`
FROM `aliquot` a
LEFT JOIN `box_path` p ON p.`id` = a.`box_id`;

CREATE VIEW `v_stock_status` AS
SELECT
  s.`id` AS `stock_id`,
  s.`kind` AS `kind`,
  s.`entity_id` AS `entity_id`,
  CASE
    WHEN COALESCE(agg.`aliquot_count`, 0) = 0 THEN 'empty'
    WHEN COALESCE(agg.`stored_count`, 0) > 0 THEN 'active'
    WHEN COALESCE(agg.`pending_count`, 0) > 0 THEN 'pending'
    WHEN COALESCE(agg.`missing_count`, 0) > 0 AND COALESCE(agg.`remaining_count`, 0) = 0 THEN 'missing'
    WHEN COALESCE(agg.`consumed_count`, 0) = COALESCE(agg.`aliquot_count`, 0) THEN 'depleted'
    ELSE 'pending'
  END AS `status`,
  COALESCE(agg.`aliquot_count`, 0) AS `aliquot_count`,
  COALESCE(agg.`stored_count`, 0) AS `stored_count`,
  COALESCE(agg.`pending_count`, 0) AS `pending_count`,
  COALESCE(agg.`missing_count`, 0) AS `missing_count`,
  COALESCE(agg.`consumed_count`, 0) AS `consumed_count`,
  agg.`total_quantity` AS `total_quantity`,
  agg.`total_unit` AS `unit`,
  (SELECT GROUP_CONCAT(`alias` ORDER BY `alias` SEPARATOR ';') FROM `alias` WHERE `object_type` = 'stock' AND `object_id` = s.`id`) AS `alias_list`
FROM `stock` s
LEFT JOIN (
  SELECT
    a.`stock_id` AS `stock_id`,
    COUNT(a.`id`) AS `aliquot_count`,
    SUM(CASE WHEN a.`status` = 'stored' THEN 1 ELSE 0 END) AS `stored_count`,
    SUM(CASE WHEN a.`status` = 'pending' THEN 1 ELSE 0 END) AS `pending_count`,
    SUM(CASE WHEN a.`status` = 'missing' THEN 1 ELSE 0 END) AS `missing_count`,
    SUM(CASE WHEN a.`status` = 'consumed' THEN 1 ELSE 0 END) AS `consumed_count`,
    SUM(CASE WHEN a.`status` NOT IN ('missing', 'consumed') THEN 1 ELSE 0 END) AS `remaining_count`,
    SUM(a.`quantity`) AS `total_quantity`,
    CASE WHEN COUNT(DISTINCT a.`unit`) = 1 THEN MAX(a.`unit`) ELSE NULL END AS `total_unit`,
    CASE WHEN COUNT(DISTINCT CASE WHEN a.`status` = 'stored' AND a.`quantity` IS NOT NULL THEN a.`unit` END) = 1 THEN SUM(CASE WHEN a.`status` = 'stored' THEN a.`quantity` ELSE NULL END) ELSE NULL END AS `quantity_stored`,
    CASE WHEN COUNT(DISTINCT CASE WHEN a.`status` = 'stored' AND a.`quantity` IS NOT NULL THEN a.`unit` END) = 1 THEN MAX(CASE WHEN a.`status` = 'stored' AND a.`quantity` IS NOT NULL THEN a.`unit` END) ELSE NULL END AS `stored_unit`
  FROM `aliquot` a
  GROUP BY a.`stock_id`
) agg ON agg.`stock_id` = s.`id`;

CREATE VIEW `v_entity_everything` AS
SELECT
  c.`id` AS `id`,
  c.`kind` AS `kind`,
  c.`created_at` AS `created_at`,
  c.`notes` AS `notes`,
  k.`name` AS `name`,
  k.`description` AS `description`,
  k.`code` AS `code`,
  k.`blurb` AS `blurb`,
  k.`count` AS `count`,
  k.`ratio` AS `ratio`,
  k.`active` AS `active`,
  k.`born` AS `born`,
  k.`seen_at` AS `seen_at`,
  k.`color` AS `color`,
  k.`seq` AS `seq`,
  k.`parent` AS `parent`,
  k.`display` AS `display`,
  k.`legacy` AS `legacy`,
  (SELECT GROUP_CONCAT(`alias` ORDER BY `alias` SEPARATOR ';') FROM `alias` WHERE `object_type` = 'entity' AND `object_id` = c.`id`) AS `alias_list`
FROM `entity_everything` k
JOIN `entity` c ON c.`id` = k.`id`;

CREATE VIEW `v_entity_item` AS
SELECT
  c.`id` AS `id`,
  c.`kind` AS `kind`,
  c.`created_at` AS `created_at`,
  c.`notes` AS `notes`,
  k.`name` AS `name`,
  k.`description` AS `description`,
  k.`category` AS `category`,
  (SELECT GROUP_CONCAT(`alias` ORDER BY `alias` SEPARATOR ';') FROM `alias` WHERE `object_type` = 'entity' AND `object_id` = c.`id`) AS `alias_list`
FROM `entity_item` k
JOIN `entity` c ON c.`id` = k.`id`;

CREATE VIEW `v_entity_plasmid` AS
SELECT
  c.`id` AS `id`,
  c.`kind` AS `kind`,
  c.`created_at` AS `created_at`,
  c.`notes` AS `notes`,
  k.`name` AS `name`,
  k.`description` AS `description`,
  k.`sequence` AS `sequence`,
  k.`length_bp` AS `length_bp`,
  k.`backbone` AS `backbone`,
  (SELECT GROUP_CONCAT(`alias` ORDER BY `alias` SEPARATOR ';') FROM `alias` WHERE `object_type` = 'entity' AND `object_id` = c.`id`) AS `alias_list`
FROM `entity_plasmid` k
JOIN `entity` c ON c.`id` = k.`id`;

CREATE VIEW `v_stock_bacterial` AS
SELECT
  c.`id` AS `id`,
  c.`kind` AS `kind`,
  c.`entity_id` AS `entity_id`,
  c.`parent_stock_id` AS `parent_stock_id`,
  c.`source_aliquot_id` AS `source_aliquot_id`,
  c.`created_at` AS `created_at`,
  c.`notes` AS `notes`,
  k.`measured_at` AS `measured_at`,
  k.`amount` AS `amount`,
  k.`strain` AS `strain`,
  k.`medium` AS `medium`,
  e.`kind` AS `entity_kind`,
  e.`name` AS `entity_name`,
  COALESCE(agg.`stored_count`, 0) AS `aliquots_stored`,
  COALESCE(agg.`aliquot_count`, 0) AS `aliquots_total`,
  agg.`quantity_stored` AS `quantity_stored`,
  agg.`stored_unit` AS `unit`,
  CASE
    WHEN COALESCE(agg.`aliquot_count`, 0) = 0 THEN 'empty'
    WHEN COALESCE(agg.`stored_count`, 0) > 0 THEN 'active'
    WHEN COALESCE(agg.`pending_count`, 0) > 0 THEN 'pending'
    WHEN COALESCE(agg.`missing_count`, 0) > 0 AND COALESCE(agg.`remaining_count`, 0) = 0 THEN 'missing'
    WHEN COALESCE(agg.`consumed_count`, 0) = COALESCE(agg.`aliquot_count`, 0) THEN 'depleted'
    ELSE 'pending'
  END AS `status`,
  (SELECT GROUP_CONCAT(`alias` ORDER BY `alias` SEPARATOR ';') FROM `alias` WHERE `object_type` = 'stock' AND `object_id` = c.`id`) AS `alias_list`
FROM `stock_bacterial` k
JOIN `stock` c ON c.`id` = k.`id`
JOIN `entity` e ON e.`id` = c.`entity_id`
LEFT JOIN (
  SELECT
    a.`stock_id` AS `stock_id`,
    COUNT(a.`id`) AS `aliquot_count`,
    SUM(CASE WHEN a.`status` = 'stored' THEN 1 ELSE 0 END) AS `stored_count`,
    SUM(CASE WHEN a.`status` = 'pending' THEN 1 ELSE 0 END) AS `pending_count`,
    SUM(CASE WHEN a.`status` = 'missing' THEN 1 ELSE 0 END) AS `missing_count`,
    SUM(CASE WHEN a.`status` = 'consumed' THEN 1 ELSE 0 END) AS `consumed_count`,
    SUM(CASE WHEN a.`status` NOT IN ('missing', 'consumed') THEN 1 ELSE 0 END) AS `remaining_count`,
    SUM(a.`quantity`) AS `total_quantity`,
    CASE WHEN COUNT(DISTINCT a.`unit`) = 1 THEN MAX(a.`unit`) ELSE NULL END AS `total_unit`,
    CASE WHEN COUNT(DISTINCT CASE WHEN a.`status` = 'stored' AND a.`quantity` IS NOT NULL THEN a.`unit` END) = 1 THEN SUM(CASE WHEN a.`status` = 'stored' THEN a.`quantity` ELSE NULL END) ELSE NULL END AS `quantity_stored`,
    CASE WHEN COUNT(DISTINCT CASE WHEN a.`status` = 'stored' AND a.`quantity` IS NOT NULL THEN a.`unit` END) = 1 THEN MAX(CASE WHEN a.`status` = 'stored' AND a.`quantity` IS NOT NULL THEN a.`unit` END) ELSE NULL END AS `stored_unit`
  FROM `aliquot` a
  GROUP BY a.`stock_id`
) agg ON agg.`stock_id` = c.`id`;

CREATE VIEW `v_stock_lot` AS
SELECT
  c.`id` AS `id`,
  c.`kind` AS `kind`,
  c.`entity_id` AS `entity_id`,
  c.`parent_stock_id` AS `parent_stock_id`,
  c.`source_aliquot_id` AS `source_aliquot_id`,
  c.`created_at` AS `created_at`,
  c.`notes` AS `notes`,
  k.`supplier` AS `supplier`,
  k.`received_on` AS `received_on`,
  e.`kind` AS `entity_kind`,
  e.`name` AS `entity_name`,
  COALESCE(agg.`stored_count`, 0) AS `aliquots_stored`,
  COALESCE(agg.`aliquot_count`, 0) AS `aliquots_total`,
  agg.`quantity_stored` AS `quantity_stored`,
  agg.`stored_unit` AS `unit`,
  CASE
    WHEN COALESCE(agg.`aliquot_count`, 0) = 0 THEN 'empty'
    WHEN COALESCE(agg.`stored_count`, 0) > 0 THEN 'active'
    WHEN COALESCE(agg.`pending_count`, 0) > 0 THEN 'pending'
    WHEN COALESCE(agg.`missing_count`, 0) > 0 AND COALESCE(agg.`remaining_count`, 0) = 0 THEN 'missing'
    WHEN COALESCE(agg.`consumed_count`, 0) = COALESCE(agg.`aliquot_count`, 0) THEN 'depleted'
    ELSE 'pending'
  END AS `status`,
  (SELECT GROUP_CONCAT(`alias` ORDER BY `alias` SEPARATOR ';') FROM `alias` WHERE `object_type` = 'stock' AND `object_id` = c.`id`) AS `alias_list`
FROM `stock_lot` k
JOIN `stock` c ON c.`id` = k.`id`
JOIN `entity` e ON e.`id` = c.`entity_id`
LEFT JOIN (
  SELECT
    a.`stock_id` AS `stock_id`,
    COUNT(a.`id`) AS `aliquot_count`,
    SUM(CASE WHEN a.`status` = 'stored' THEN 1 ELSE 0 END) AS `stored_count`,
    SUM(CASE WHEN a.`status` = 'pending' THEN 1 ELSE 0 END) AS `pending_count`,
    SUM(CASE WHEN a.`status` = 'missing' THEN 1 ELSE 0 END) AS `missing_count`,
    SUM(CASE WHEN a.`status` = 'consumed' THEN 1 ELSE 0 END) AS `consumed_count`,
    SUM(CASE WHEN a.`status` NOT IN ('missing', 'consumed') THEN 1 ELSE 0 END) AS `remaining_count`,
    SUM(a.`quantity`) AS `total_quantity`,
    CASE WHEN COUNT(DISTINCT a.`unit`) = 1 THEN MAX(a.`unit`) ELSE NULL END AS `total_unit`,
    CASE WHEN COUNT(DISTINCT CASE WHEN a.`status` = 'stored' AND a.`quantity` IS NOT NULL THEN a.`unit` END) = 1 THEN SUM(CASE WHEN a.`status` = 'stored' THEN a.`quantity` ELSE NULL END) ELSE NULL END AS `quantity_stored`,
    CASE WHEN COUNT(DISTINCT CASE WHEN a.`status` = 'stored' AND a.`quantity` IS NOT NULL THEN a.`unit` END) = 1 THEN MAX(CASE WHEN a.`status` = 'stored' AND a.`quantity` IS NOT NULL THEN a.`unit` END) ELSE NULL END AS `stored_unit`
  FROM `aliquot` a
  GROUP BY a.`stock_id`
) agg ON agg.`stock_id` = c.`id`;

CREATE VIEW `v_stock_prep` AS
SELECT
  c.`id` AS `id`,
  c.`kind` AS `kind`,
  c.`entity_id` AS `entity_id`,
  c.`parent_stock_id` AS `parent_stock_id`,
  c.`source_aliquot_id` AS `source_aliquot_id`,
  c.`created_at` AS `created_at`,
  c.`notes` AS `notes`,
  k.`batch` AS `batch`,
  e.`kind` AS `entity_kind`,
  e.`name` AS `entity_name`,
  COALESCE(agg.`stored_count`, 0) AS `aliquots_stored`,
  COALESCE(agg.`aliquot_count`, 0) AS `aliquots_total`,
  agg.`quantity_stored` AS `quantity_stored`,
  agg.`stored_unit` AS `unit`,
  CASE
    WHEN COALESCE(agg.`aliquot_count`, 0) = 0 THEN 'empty'
    WHEN COALESCE(agg.`stored_count`, 0) > 0 THEN 'active'
    WHEN COALESCE(agg.`pending_count`, 0) > 0 THEN 'pending'
    WHEN COALESCE(agg.`missing_count`, 0) > 0 AND COALESCE(agg.`remaining_count`, 0) = 0 THEN 'missing'
    WHEN COALESCE(agg.`consumed_count`, 0) = COALESCE(agg.`aliquot_count`, 0) THEN 'depleted'
    ELSE 'pending'
  END AS `status`,
  (SELECT GROUP_CONCAT(`alias` ORDER BY `alias` SEPARATOR ';') FROM `alias` WHERE `object_type` = 'stock' AND `object_id` = c.`id`) AS `alias_list`
FROM `stock_prep` k
JOIN `stock` c ON c.`id` = k.`id`
JOIN `entity` e ON e.`id` = c.`entity_id`
LEFT JOIN (
  SELECT
    a.`stock_id` AS `stock_id`,
    COUNT(a.`id`) AS `aliquot_count`,
    SUM(CASE WHEN a.`status` = 'stored' THEN 1 ELSE 0 END) AS `stored_count`,
    SUM(CASE WHEN a.`status` = 'pending' THEN 1 ELSE 0 END) AS `pending_count`,
    SUM(CASE WHEN a.`status` = 'missing' THEN 1 ELSE 0 END) AS `missing_count`,
    SUM(CASE WHEN a.`status` = 'consumed' THEN 1 ELSE 0 END) AS `consumed_count`,
    SUM(CASE WHEN a.`status` NOT IN ('missing', 'consumed') THEN 1 ELSE 0 END) AS `remaining_count`,
    SUM(a.`quantity`) AS `total_quantity`,
    CASE WHEN COUNT(DISTINCT a.`unit`) = 1 THEN MAX(a.`unit`) ELSE NULL END AS `total_unit`,
    CASE WHEN COUNT(DISTINCT CASE WHEN a.`status` = 'stored' AND a.`quantity` IS NOT NULL THEN a.`unit` END) = 1 THEN SUM(CASE WHEN a.`status` = 'stored' THEN a.`quantity` ELSE NULL END) ELSE NULL END AS `quantity_stored`,
    CASE WHEN COUNT(DISTINCT CASE WHEN a.`status` = 'stored' AND a.`quantity` IS NOT NULL THEN a.`unit` END) = 1 THEN MAX(CASE WHEN a.`status` = 'stored' AND a.`quantity` IS NOT NULL THEN a.`unit` END) ELSE NULL END AS `stored_unit`
  FROM `aliquot` a
  GROUP BY a.`stock_id`
) agg ON agg.`stock_id` = c.`id`;

CREATE VIEW `v_box_grid96` AS
SELECT
  c.`id` AS `id`,
  c.`kind` AS `kind`,
  c.`name` AS `name`,
  c.`parent_box_id` AS `parent_box_id`,
  c.`parent_slot` AS `parent_slot`,
  c.`created_at` AS `created_at`,
  c.`notes` AS `notes`,
  (SELECT GROUP_CONCAT(`alias` ORDER BY `alias` SEPARATOR ';') FROM `alias` WHERE `object_type` = 'box' AND `object_id` = c.`id`) AS `alias_list`
FROM `box_grid96` k
JOIN `box` c ON c.`id` = k.`id`;

CREATE VIEW `v_box_grid9x9` AS
SELECT
  c.`id` AS `id`,
  c.`kind` AS `kind`,
  c.`name` AS `name`,
  c.`parent_box_id` AS `parent_box_id`,
  c.`parent_slot` AS `parent_slot`,
  c.`created_at` AS `created_at`,
  c.`notes` AS `notes`,
  (SELECT GROUP_CONCAT(`alias` ORDER BY `alias` SEPARATOR ';') FROM `alias` WHERE `object_type` = 'box' AND `object_id` = c.`id`) AS `alias_list`
FROM `box_grid9x9` k
JOIN `box` c ON c.`id` = k.`id`;

CREATE VIEW `v_box_namedbox` AS
SELECT
  c.`id` AS `id`,
  c.`kind` AS `kind`,
  c.`name` AS `name`,
  c.`parent_box_id` AS `parent_box_id`,
  c.`parent_slot` AS `parent_slot`,
  c.`created_at` AS `created_at`,
  c.`notes` AS `notes`,
  (SELECT GROUP_CONCAT(`alias` ORDER BY `alias` SEPARATOR ';') FROM `alias` WHERE `object_type` = 'box' AND `object_id` = c.`id`) AS `alias_list`
FROM `box_namedbox` k
JOIN `box` c ON c.`id` = k.`id`;

CREATE VIEW `v_box_rack` AS
SELECT
  c.`id` AS `id`,
  c.`kind` AS `kind`,
  c.`name` AS `name`,
  c.`parent_box_id` AS `parent_box_id`,
  c.`parent_slot` AS `parent_slot`,
  c.`created_at` AS `created_at`,
  c.`notes` AS `notes`,
  (SELECT GROUP_CONCAT(`alias` ORDER BY `alias` SEPARATOR ';') FROM `alias` WHERE `object_type` = 'box' AND `object_id` = c.`id`) AS `alias_list`
FROM `box_rack` k
JOIN `box` c ON c.`id` = k.`id`;

CREATE VIEW `v_box_shelf` AS
SELECT
  c.`id` AS `id`,
  c.`kind` AS `kind`,
  c.`name` AS `name`,
  c.`parent_box_id` AS `parent_box_id`,
  c.`parent_slot` AS `parent_slot`,
  c.`created_at` AS `created_at`,
  c.`notes` AS `notes`,
  (SELECT GROUP_CONCAT(`alias` ORDER BY `alias` SEPARATOR ';') FROM `alias` WHERE `object_type` = 'box' AND `object_id` = c.`id`) AS `alias_list`
FROM `box_shelf` k
JOIN `box` c ON c.`id` = k.`id`;

CREATE VIEW `v_box_slotlessbox` AS
SELECT
  c.`id` AS `id`,
  c.`kind` AS `kind`,
  c.`name` AS `name`,
  c.`parent_box_id` AS `parent_box_id`,
  c.`parent_slot` AS `parent_slot`,
  c.`created_at` AS `created_at`,
  c.`notes` AS `notes`,
  (SELECT GROUP_CONCAT(`alias` ORDER BY `alias` SEPARATOR ';') FROM `alias` WHERE `object_type` = 'box' AND `object_id` = c.`id`) AS `alias_list`
FROM `box_slotlessbox` k
JOIN `box` c ON c.`id` = k.`id`;
