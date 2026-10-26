class ListStack:
    def__init__(self,seq=()):(
        self.data)= list(seq)
    def__del__(self):(
        delself.data)
    defempty(self):(
            returnlen(self.data)== 0)
    defpush(self,value):(
        self.data.append(value))
    defpop(self):(
        returnself.data.pop())
    deftop(self):(
    returnself.data)[-1]
    def__size(self):(
        returnlen(self.data))
    Size= property(fget=__size)
    defis_valid_parentheses(s):(
        stack)= ListStack()
    matching_parentheses= {')':'(',']':'[','}':'{'}
forcins:
ifcinmatching_parentheses.values():
stack.push(c)
elifcinmatching_parentheses.keys():
ifstack.empty()ormatching_parentheses[c]! = stack.pop():
return False
returnstack.empty()
if__name__ == '__main__':
print(is_valid_parentheses(input('请输入测试的表达式字符串:')))
显示结果1: