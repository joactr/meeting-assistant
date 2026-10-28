UPDATE meetings SET extensions_json=json_remove(extensions_json,'$.template_id','$.template_snapshot','$.legacy_template');
DELETE FROM document_history WHERE collection IN('templates','template_versions');
ALTER TABLE meetings DROP COLUMN template_version_id;
ALTER TABLE projects DROP COLUMN default_template_id;
DROP TABLE template_versions;
DROP TABLE templates;
PRAGMA user_version=4;
