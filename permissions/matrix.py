PERMISSION_MATRIX = {
    "ADMIN": [
        "CREATE_USER",
        "CHANGE_ROLE",
        "DELETE_ARCHIVE",
        "SIGN_ARCHIVE",
        "VIEW_TIMELINE"
    ],
    "USER": [
        "CREATE_REQUEST",
        "UPLOAD_ATTACHMENT",
        "VIEW_OWN_REQUESTS"
    ],
    "dept_head": [
        "APPROVE_REQUEST"
    ],
    "finance": [
        "APPROVE_REQUEST"
    ],
    "secretary_general": [
        "FINAL_APPROVE"
    ]
}
