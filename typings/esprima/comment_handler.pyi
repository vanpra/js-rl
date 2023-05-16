"""
This type stub file was generated by pyright.
"""

from .nodes import Node
from .objects import Object

class Comment(Node):
    def __init__(self, type, value, range=..., loc=...) -> None: ...

class Entry(Object):
    def __init__(self, comment, start) -> None: ...

class NodeInfo(Object):
    def __init__(self, node, start) -> None: ...

class CommentHandler:
    def __init__(self) -> None: ...
    def insertInnerComments(self, node, metadata): ...
    def findTrailingComments(self, metadata): ...
    def findLeadingComments(self, metadata): ...
    def visitNode(self, node, metadata): ...
    def visitComment(self, node, metadata): ...
    def visit(self, node, metadata): ...
