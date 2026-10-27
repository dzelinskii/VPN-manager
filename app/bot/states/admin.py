"""FSM states for admin flows."""

from aiogram.fsm.state import State, StatesGroup


class ServerManagement(StatesGroup):
    """Server management states."""

    waiting_for_name = State()
    waiting_for_ip_address = State()
    confirm_add_offline = State()

    # SSH setup flow
    waiting_for_ssh_port = State()
    waiting_for_ssh_user = State()
    waiting_for_ssh_auth = State()  # Password or Key

    # Legacy / XUI Panel
    waiting_for_base_url = State()
    waiting_for_panel_path = State()
    waiting_for_subscription_path = State()
    waiting_for_subscription_json_path = State()
    waiting_for_username = State()
    waiting_for_password = State()
    waiting_for_verify_ssl = State()
    confirm_delete = State()

    # Server editing states
    waiting_for_edit_name = State()
    waiting_for_edit_ip_address = State()
    waiting_for_edit_ssh_port = State()
    waiting_for_edit_ssh_user = State()
    waiting_for_edit_ssh_auth = State()

    waiting_for_edit_base_url = State()
    waiting_for_edit_panel_path = State()
    waiting_for_edit_subscription_path = State()
    waiting_for_edit_subscription_json_path = State()
    waiting_for_edit_username = State()
    waiting_for_edit_password = State()
    waiting_for_edit_verify_ssl = State()
    waiting_for_edit_api_token = State()

    # Desync restore
    waiting_for_restore_file = State()


class UserManagement(StatesGroup):
    """User management states."""

    waiting_for_name = State()
    waiting_for_telegram_id = State()
    confirm_delete = State()
    waiting_for_new_name = State()


class ClientManagement(StatesGroup):
    """Client management states."""

    waiting_for_name = State()
    waiting_for_email = State()
    waiting_for_telegram_id = State()
    waiting_for_telegram_username = State()
    waiting_for_new_name = State()
    waiting_for_new_telegram_id = State()
    confirm_delete = State()

    # Add inbound to subscription states
    waiting_for_inbound_server = State()
    waiting_for_inbound_selection = State()

    # Search states
    waiting_for_search_query = State()
    waiting_for_search_field = State()

    # Notes state
    waiting_for_notes = State()


class ManualImport(StatesGroup):
    """Мастер импорта созданного вручную клиента с панели."""

    choosing_client = State()
    entering_new_name = State()


class SubscriptionManagement(StatesGroup):
    """Subscription management states."""

    # Select client
    waiting_for_client_selection = State()

    # Select server (multiple selection)
    waiting_for_server_selection = State()

    # Select inbound (multiple selection)
    waiting_for_inbound_selection = State()
    inbounds_multi_select_mode = State()  # Multi-selection mode
    inbounds_multi_confirm_action = State()  # Confirm multi-selection action

    # Subscription parameters (creation flow)
    waiting_for_subscription_name = State()
    waiting_for_traffic_limit = State()
    waiting_for_expiry_days = State()
    confirm_creation = State()

    # Subscription editing (separate states to avoid conflict with creation flow)
    editing_name = State()
    editing_traffic = State()
    editing_expiry = State()
    editing_notes = State()
    editing_subscription_price = State()
    waiting_for_add_days = State()
    waiting_for_mtproxy_domain = State()


class SubscriptionRebuild(StatesGroup):
    """Subscription rebuild/token reuse states."""

    waiting_for_mode_selection = State()
    waiting_for_template_selection = State()
    waiting_for_server_selection = State()
    waiting_for_inbound_selection = State()
    inbounds_multi_select_mode = State()
    waiting_for_subscription_name = State()
    waiting_for_traffic_limit = State()
    waiting_for_expiry_days = State()
    confirm_rebuild = State()


class ExportData(StatesGroup):
    """Export data states."""

    waiting_for_format = State()


class TemplateManagement(StatesGroup):
    """Template management states."""

    # Template creation states
    waiting_for_template_name = State()
    waiting_for_template_description = State()
    waiting_for_default_traffic = State()
    waiting_for_default_expiry = State()
    waiting_for_template_notes = State()

    # Template editing states
    editing_template_name = State()
    editing_template_description = State()
    editing_default_traffic = State()
    editing_default_expiry = State()
    editing_template_notes = State()
    editing_template_price = State()
    editing_template_menu = State()  # For showing edit menu

    # Template inbound management states
    waiting_for_server_selection = State()  # For selecting server in edit mode
    waiting_for_inbound_selection = State()
    inbounds_multi_select_mode = State()  # Multi-selection mode for template inbounds
    inbounds_multi_confirm_action = State()  # Confirm multi-selection action for templates
    confirm_remove_inbound = State()

    # Template subscription creation states
    waiting_for_client_selection = State()
    waiting_for_template_selection = (
        State()
    )  # For creating subscription from template for specific client
    waiting_for_subscription_name = State()
    waiting_for_search_query = State()  # For client search

    # Template deletion
    confirm_delete_template = State()


class BroadcastManagement(StatesGroup):
    """Broadcast messages to users states."""

    waiting_for_message = State()
    confirm_broadcast = State()


class FirstSetup(StatesGroup):
    """Shared first-install setup states (firewall policy + SSH port)."""

    waiting_for_firewall_policy = State()
    waiting_for_ssh_port_choice = State()
    waiting_for_ssh_port = State()


class AWGInstall(StatesGroup):
    """AWG installation flow states."""

    waiting_for_port = State()
    waiting_for_obfuscation_mode = State()
    waiting_for_obfuscation_params = State()
    confirm_install = State()


class XUIInstall(StatesGroup):
    """3x-ui installation flow states."""

    waiting_for_domain = State()
    waiting_for_caddy_port = State()
    waiting_for_paths_mode = State()
    waiting_for_web_path = State()
    waiting_for_sub_path = State()
    waiting_for_sub_json_path = State()
    waiting_for_auth_mode = State()
    waiting_for_api_token = State()
    waiting_for_credentials_mode = State()
    waiting_for_username = State()
    waiting_for_password = State()
    waiting_for_inbound_range = State()
    confirm_install = State()


class MTProxyInstall(StatesGroup):
    """MTProxy installation flow states."""

    waiting_for_implementation = State()
    waiting_for_port = State()
    waiting_for_domain = State()
    waiting_for_max_connections = State()
    confirm_install = State()
