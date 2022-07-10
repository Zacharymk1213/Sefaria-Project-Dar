#!/bin/python3
import re
#This script picks off where the Sefaria Export leaves off.
#It assumes you have a Sefaria document with Some Info then
#eg. Chapter 1 then some text then chapter 2 followed by some
#text etc. This Script will complete the Dar conversion.

#For things like eg. Mishnayot there's no א,ב,ג for eg. mishnayot and pesukim(verses).
#This is a Gematria calculating function for when we need to add this
#we're basically never using sofit characters so I felt safe removing them
# Define the dictionary we'll be using unicode escape characters see 
#https://en.wikipedia.org/wiki/Unicode_and_HTML_for_the_Hebrew_alphabet for reference

def convert_to_hebrew(number):
	#This function has been verified to work. Don't touch.It's for converting numbers to Gematria
	#this dict is more for reference. It's copied from an old script and slightly modified
	dict = {1:'\u05D0',2:'\u05D1' ,3: '\u05D2',4:'\u05D3',5:'\u05D4',6:'\u05D5',7:'\u05D6',8:'\u05D7',9:'\u05D8',10:'\u05D9', 20: '\u05DB',30:'\u05DC',40:'\u05DE',50:'\u05E0',60:'\u05E1',70:'\u05E2',80:'\u05E4',90:'\u05E6',100:'\u05E7',200:'\u05E8',300:'\u05E9',400:'\u05EA',' ':' '}
	#divide the number by 400, then 300 etc. to arrive at the Gematria
	gematria=""
	numbertest = 0
	while (number >= 400):
		gematria+='\u05EA'
		number -= 400
	while (number >= 300):
		gematria+='\u05E9'
		number -= 300
	while (number >= 200):
		gematria+='\u05E8'
		number -= 200
	while (number >= 100):
		gematria+='\u05E7'
		number -= 100
	while (number >= 90):
		gematria+='\u05E6'
		number -= 90
	while (number >= 80):
		gematria+='\u05E4'
		number -= 80
	while (number >= 70):
		gematria+='\u05E2'
		number -= 70
	while (number >= 60):
		gematria+='\u05E1'
		number -= 60
	while (number >= 50):
		gematria+='\u05E0'
		number -= 50
	while (number >= 40):
		gematria+='\u05DE'
		number -= 40
	while (number >= 30):
		gematria+='\u05DC'
		number -= 30
	while (number >= 20):
		gematria+='\u05DB'
		number -= 20
	while (number >= 10):
		gematria+='\u05D9'
		number -= 10
	while (number >= 9):
		gematria+='\u05D8'
		number -= 9
	while (number >= 8):
		gematria+='\u05D7'
		number -= 8
	while (number >= 7):
		gematria+='\u05D6'
		number -= 7
	while (number >= 6):
		gematria+='\u05D5'
		number -= 6
	while (number >= 5):
		gematria+='\u05D4'
		number -= 5
	while (number >= 4):
		gematria+='\u05D3'
		number -= 4
	while (number >= 3):
		gematria+='\u05D2'
		number -= 3
	while (number >= 2):
		gematria+='\u05D1'
		number -= 2
	while (number >= 1):
		gematria+='\u05D0'
		number -= 1
	return gematria

#copied from Stack overflow and modified

def inplace_change(filename, old_string, new_string):
    #the goal here is to replace chapter $whatever_number in the genesis-exported file
    #with פרק $whatever_the_gematria_(numerical_value)_of_that_number_is
    # Safely read the input filename using 'with'
    #copied from python IRC
    #<JAA> zachary: Breaking it down, it matches: start of the string (^), literal string 'Chapter', decimal digit (\d), one or more times (+), end of the string ($)
    #<JAA> zachary: r'^Chapter\d+$' will match 'Chapter3' etc.
    regex = re.compile(old_string)
    is_top_removed = False
    with open(filename,'r') as f:
        s = f.read()
        s_list = s.split()
        
        
        if old_string not in s_list:
            print('"{old_string}" not found in {filename}.'.format(**locals()))
            return
        f.close()
    # Safely write the changed content, if found in the file
    with open(filename, 'r+') as f:
        #before proceeding double check that the string occurs and is followed by a number
        #breakpoint()
        """if old_string in s_list:
            for old_string in s_list:
                the_index = s_list.index(old_string)
            try:
                int(s_list(the_index+1))
            except:
                return 1
        """
        #print('Changing "{old_string}" to "{new_string}" in {filename}'.format(**locals()))
        print("old_string is:" + old_string)
        #[22:08] <BrenBarn> zachary: are you saying you have a list that may have a certain string in it several times, and you want to replace those positions in the list with the new string?
        #[22:08] <BrenBarn> if that is the case, you need to do something like `for ix, item in enumerate(slist): if item == old_string: slist[ix] = new_string`
        #[22:14] <BrenBarn> zachary: doing something like `for x in my_list` just means "loop over everything in `my_list` and assign each item to `x` as we go through the loop".  The name `x` is arbitrary, it doesn't affect the loop at all.  So doing something like `for old_string in slist` doesn't mean "find all occurrences of old_string in slist", it just means "loop over everything in slist and call it old_string"
        #breakpoint()
        for ix,item in enumerate(s_list):
            """print("old_string is:" + old_string)
            the_index = s_list.index(old_string)
            print("old_string is:" + old_string)
            """
            #replace the perek
            if item == old_string:

                if is_top_removed == False:
                    #do a loop of popping at [0] until the item is פרק
                    while is_top_removed == False:
                      
                        if s_list[0] == "Chapter":
                            is_top_removed=True
                            #resets the index
                            ix=0
                            break
                        s_list.pop(0)
                s_list[ix] = "\n" + "%" + "פרק"        
            #print("old_string is:" + old_string)
                try:
                    get_number = s_list[ix+1]
                    number = convert_to_hebrew(int(get_number))
                    #s_list[the_index] = "פרק"
                    s_list[ix+1] = number
                    s_list.insert(ix+2,"\n")
                except:
                    return 1

            #if character is : then add a list item that consists of a newline character
            #before doing check if there's a {פ} if so do it after that

            if item[-1] == "\u05C3":
                try:
                    if s_list[ix+1] == "{פ}":
                        if s_list[ix+2] == "{ס}":
                            s_list.insert(ix+3,"\n")
                            
                        else:
            	            s_list.insert(ix+2,"\n")
                    elif s_list[ix+1] == "{ס}":
                        s_list.insert(ix+2,"\n")
                    else:
                        s_list.insert(ix+1,"\n")
                except:
                    pass
            #remove the *׀* stuff
            #breakpoint()
            if item == "\u002A" + "\u05C0" + "\u002A":
                s_list[ix] = ""
            if item == "|":
                s_list[ix] = ""
            #print("old_string is:" + old_string)
         
        #https://stackoverflow.com/questions/5618878/how-to-convert-list-to-string
        final=' '.join(s_list)

        f.write(final)

